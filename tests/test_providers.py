import json

import httpx
import pytest

from folder_routing.http import ApiError
from folder_routing.providers import CapabilityUnavailable, WriteRejected, WriteUncertain
from folder_routing.providers.fathom import FathomFolderWriter, FathomReader
from folder_routing.providers.granola import GranolaMcpWriter, GranolaReader
from folder_routing.providers.mcp_client import McpHttpClient

NO_SLEEP = lambda s: None


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_granola_list_notes_follows_cursor_across_pages():
    seen = []

    def handler(req):
        cursor = req.url.params.get("cursor")
        seen.append(cursor)
        page = {None: (["not_A", "not_B"], "c1"), "c1": (["not_C"], "c2"), "c2": (["not_D"], None)}[cursor]
        return httpx.Response(200, json={"notes": [{"id": i} for i in page[0]], "hasMore": page[1] is not None,
                                         "cursor": page[1]})

    ids = [n["id"] for n in GranolaReader("k", client(handler), sleep=NO_SLEEP).iter_notes(updated_after="x")]
    assert ids == ["not_A", "not_B", "not_C", "not_D"]
    assert seen == [None, "c1", "c2"]


def test_fathom_list_meetings_follows_next_cursor():
    def handler(req):
        cursor = req.url.params.get("cursor")
        assert req.headers["x-api-key"] == "k"
        items, nxt = {None: ([1, 2], "n1"), "n1": ([3], None)}[cursor]
        return httpx.Response(200, json={"items": [{"recording_id": i} for i in items], "next_cursor": nxt})

    reader = FathomReader("k", httpx.Client(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "k"}),
                          sleep=NO_SLEEP)
    assert [m["recording_id"] for m in reader.iter_meetings()] == [1, 2, 3]


def test_rate_limit_then_success():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"folders": [], "hasMore": False, "cursor": None})

    assert list(GranolaReader("k", client(handler), sleep=NO_SLEEP).iter_folders()) == []
    assert len(calls) == 3


def test_server_errors_exhaust_into_retryable_api_error():
    reader = GranolaReader("k", client(lambda req: httpx.Response(503)), sleep=NO_SLEEP)
    with pytest.raises(ApiError) as e:
        reader.get_note("not_A")
    assert e.value.retryable and e.value.status == 503


def test_permanent_4xx_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(403)

    with pytest.raises(ApiError) as e:
        GranolaReader("k", client(handler), sleep=NO_SLEEP).list_webhook_endpoints()
    assert not e.value.retryable and len(calls) == 1


def test_deleted_or_inaccessible_note_is_none():
    reader = GranolaReader("k", client(lambda req: httpx.Response(404)), sleep=NO_SLEEP)
    assert reader.get_note("not_A") == (None, None)


def test_transcript_too_large_falls_back_to_paginated_endpoint():
    def handler(req):
        path = req.url.path
        if path == "/v1/notes/not_A" and req.url.params.get("include") == "transcript":
            return httpx.Response(413, json={"code": "TRANSCRIPT_TOO_LARGE"})
        if path == "/v1/notes/not_A":
            return httpx.Response(200, json={"id": "not_A"})
        cursor = req.url.params.get("cursor")
        items = [{"speaker": {"attribution": "them"}, "text": "x"}]
        return httpx.Response(200, json={"transcript": items, "hasMore": cursor is None,
                                         "cursor": None if cursor else "p2"})

    note, transcript = GranolaReader("k", client(handler), sleep=NO_SLEEP).get_note("not_A")
    assert note["id"] == "not_A" and len(transcript) == 2


def test_fathom_folder_operations_are_unavailable():
    with pytest.raises(CapabilityUnavailable):
        FathomFolderWriter().add("1", "folder")
    with pytest.raises(CapabilityUnavailable):
        FathomReader("k", client(lambda r: httpx.Response(500))).folder_membership(1)


class Tokens:
    def __init__(self):
        self.token, self.refreshed = "t1", 0

    def access_token(self):
        return self.token

    def refresh(self):
        self.refreshed += 1
        self.token = "t2"


def mcp_server(tool_handler, sse=False, expire_first=False):
    state = {"expired": expire_first, "auth": []}

    def handler(req):
        state["auth"].append(req.headers["authorization"])
        if state["expired"]:
            state["expired"] = False
            return httpx.Response(401)
        msg = json.loads(req.content)
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
        else:
            result = tool_handler(msg["params"]["name"], msg["params"]["arguments"])
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if sse:
            return httpx.Response(200, headers={"content-type": "text/event-stream", "mcp-session-id": "s1"},
                                  text=f"event: message\ndata: {json.dumps(reply)}\n\n")
        return httpx.Response(200, headers={"mcp-session-id": "s1"}, json=reply)

    return handler, state


def text_result(obj, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(obj)}], "isError": is_error}


@pytest.mark.parametrize("sse", [False, True])
def test_mcp_add_and_folder_lookup(sse):
    folders = {"folders": [{"id": "uuid-1", "title": "PLSA", "visibility": "private",
                            "audience": "shared with other people", "parent_folder_id": None, "role": "owner"}]}

    def tools(name, args):
        if name == "list_meeting_folders":
            return text_result(folders)
        assert name == "add_note_to_folder" and args == {"note_id": "not_A", "folder_id": "uuid-1"}
        return text_result({"result": "already_in_folder"})

    handler, _ = mcp_server(tools, sse=sse)
    writer = GranolaMcpWriter(McpHttpClient("https://mcp.test/mcp", Tokens(), client(handler)))
    f = writer.get_folder("uuid-1")
    assert (f.title, f.visibility, f.role) == ("PLSA", "private", "owner")
    assert writer.add("not_A", "uuid-1").outcome == "already_in_folder"


def test_mcp_refreshes_token_once_on_401():
    handler, state = mcp_server(lambda n, a: text_result({"folders": []}), expire_first=True)
    tokens = Tokens()
    GranolaMcpWriter(McpHttpClient("https://mcp.test/mcp", tokens, client(handler))).get_folder("x")
    assert tokens.refreshed == 1 and state["auth"][:2] == ["Bearer t1", "Bearer t2"]


def test_mcp_permission_refusal_is_rejected_not_retried():
    handler, _ = mcp_server(lambda n, a: text_result({"error": "Only notes you created or collaborate on can be added"}, True))
    writer = GranolaMcpWriter(McpHttpClient("https://mcp.test/mcp", Tokens(), client(handler)))
    with pytest.raises(WriteRejected):
        writer.add("not_A", "uuid-1")


def test_mcp_timeout_during_write_is_uncertain():
    def handler(req):
        msg = json.loads(req.content)
        if msg.get("method") == "tools/call":
            raise httpx.ReadTimeout("slow")
        if "id" not in msg:
            return httpx.Response(202)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {}})

    writer = GranolaMcpWriter(McpHttpClient("https://mcp.test/mcp", Tokens(), client(handler)))
    with pytest.raises(WriteUncertain):
        writer.add("not_A", "uuid-1")


def test_mcp_folder_listing_parses_ids_and_pagination_only():
    pages = {
        None: '<meetings_data><meeting id="11111111-1111-4111-8111-111111111111" title="x"/></meetings_data>'
              '<pagination has_more="true" next_cursor="abc" total_count="2" />',
        "abc": '<meeting id="22222222-2222-4222-8222-222222222222" title="y"/><pagination has_more="false" />',
    }

    def tools(name, args):
        return {"content": [{"type": "text", "text": pages[args.get("cursor")]}]}

    handler, _ = mcp_server(tools)
    writer = GranolaMcpWriter(McpHttpClient("https://mcp.test/mcp", Tokens(), client(handler)))
    assert writer.list_folder_note_uuids("uuid-1", "2020-01-01", "2026-10-30") == {
        "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"}


def test_persistent_mcp_401_is_auth_failure_not_a_blocked_note():
    from folder_routing.providers.oauth import TokenUnavailable
    tokens = Tokens()
    writer = GranolaMcpWriter(McpHttpClient("https://mcp.test/mcp", tokens, client(lambda r: httpx.Response(401))))
    with pytest.raises(TokenUnavailable):
        writer.add("not_A", "uuid-1")
    assert tokens.refreshed == 1
