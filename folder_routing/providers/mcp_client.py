"""Minimal MCP client over Streamable HTTP (JSON-RPC 2.0 via POST).

Write calls are never retried here: a request that may have reached the server
surfaces as WriteUncertain so the caller verifies membership before trying again.
"""
import itertools
import json

import httpx

from . import WriteUncertain
from .oauth import TokenUnavailable

PROTOCOL_VERSION = "2025-06-18"


class McpError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _parse_sse(text, want_id):
    for block in text.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if not data:
            continue
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == want_id:
            return msg
    return None


class McpHttpClient:
    def __init__(self, url, token_provider, client=None, timeout=30):
        self.url = url
        self.tokens = token_provider
        self.client = client or httpx.Client(timeout=timeout)
        self.session_id = None
        self.ids = itertools.count(1)

    def _headers(self):
        h = {"Authorization": f"Bearer {self.tokens.access_token()}",
             "Accept": "application/json, text/event-stream",
             "Content-Type": "application/json",
             "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, method, params=None, notify=False, is_write=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = next(self.ids)
        for attempt in (1, 2):
            try:
                resp = self.client.post(self.url, json=msg, headers=self._headers())
            except httpx.ConnectError:
                raise McpError("connect_error", retryable=True) from None  # never reached the server
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                if is_write:
                    raise WriteUncertain(f"mcp_{type(e).__name__}") from None
                raise McpError(f"mcp_{type(e).__name__}", retryable=True) from None
            if resp.status_code == 401 and attempt == 1:
                self.tokens.refresh()
                continue
            if resp.status_code == 404 and self.session_id and attempt == 1 and method != "initialize":
                self.session_id = None  # session expired; re-initialize once
                self.initialize()
                continue
            break
        if resp.status_code in (401, 403):
            # Auth, not the note: stop all writes rather than marking this note blocked.
            raise TokenUnavailable(f"mcp_http_{resp.status_code}")
        if resp.status_code >= 500 or resp.status_code == 429:
            if is_write:
                raise WriteUncertain(f"mcp_http_{resp.status_code}")
            raise McpError(f"mcp_http_{resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise McpError(f"mcp_http_{resp.status_code}")
        if resp.headers.get("mcp-session-id"):
            self.session_id = resp.headers["mcp-session-id"]
        if notify:
            return None
        ctype = resp.headers.get("content-type", "")
        reply = _parse_sse(resp.text, msg["id"]) if "text/event-stream" in ctype else resp.json()
        if not reply:
            if is_write:
                raise WriteUncertain("mcp_no_reply")
            raise McpError("mcp_no_reply", retryable=True)
        if "error" in reply:
            raise McpError(f"mcp_rpc_{(reply['error'] or {}).get('code')}")
        return reply.get("result") or {}

    def initialize(self):
        result = self._post("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "notes-sync-folder-routing", "version": "1"},
        })
        self._post("notifications/initialized", notify=True)
        return result

    def call_tool(self, name, arguments, is_write=False):
        if self.session_id is None:
            self.initialize()
        return self._post("tools/call", {"name": name, "arguments": arguments}, is_write=is_write)


def tool_payload(result):
    """structuredContent if present, else the first text block parsed as JSON, else the raw text."""
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for block in result.get("content") or []:
        if block.get("type") == "text":
            try:
                return json.loads(block.get("text") or "")
            except ValueError:
                return {"text": block.get("text") or ""}
    return {}
