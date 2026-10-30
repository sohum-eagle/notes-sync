import json
import os
from dataclasses import replace

import httpx
import pytest

os.environ.setdefault("GRANOLA_API_KEY", "test-key")  # granola_sync reads it at import

from folder_routing import runtime  # noqa: E402
from tests.fakes import fathom_meeting, make_settings, sign  # noqa: E402

PAYLOAD = fathom_meeting(555, title="Weekly sync", invitees=[("J", "jsuiter@plsaengineering.com")],
                         speakers=[("J", "jsuiter@plsaengineering.com", 3)])
BODY = json.dumps(PAYLOAD).encode()


@pytest.fixture
def app(store, monkeypatch):
    import main
    runtime.configure(settings=make_settings(writes=False), store=store)
    attio = []
    monkeypatch.setattr(main, "_sync_note_to_people", lambda *a, **k: attio.append((a, k)) or ["J"])
    return main.app.test_client(), attio, store


def post(client, body=BODY, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(sign(body) if headers is None else headers)
    return client.post("/webhook", data=body, headers=h)


def count(store, table):
    with store.conn() as c:
        return c.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_signed_webhook_enqueues_and_attio_call_is_unchanged(app):
    client, attio, store = app
    r = post(client)
    assert r.status_code == 200 and r.json == {"ok": True, "posted": 1}
    assert count(store, "fr_jobs") == 1
    args, kwargs = attio[0]
    assert args == (PAYLOAD["calendar_invitees"], "Weekly sync", "## Summary\nSomething",
                    PAYLOAD["url"]) and kwargs == {"source": "Fathom"}


def test_duplicate_webhook_delivery_enqueues_once(app):
    client, attio, store = app
    headers = sign(BODY, msg_id="msg_dup")
    post(client, headers=headers)
    post(client, headers=headers)
    assert count(store, "fr_jobs") == 1 and count(store, "fr_webhook_deliveries") == 1
    assert len(attio) == 2  # Attio path keeps its own existing dedup


def test_unsigned_or_forged_webhook_is_not_routed_but_attio_unchanged(app):
    client, attio, store = app
    post(client, headers={})
    post(client, headers=dict(sign(BODY), **{"webhook-signature": "v1,forged"}))
    assert count(store, "fr_jobs") == 0 and len(attio) == 2


def test_routing_failure_does_not_affect_attio(app, monkeypatch):
    client, attio, store = app
    monkeypatch.setattr(store, "record_delivery", lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    assert post(client).status_code == 200 and len(attio) == 1


def test_attio_failure_does_not_affect_routing(app, monkeypatch):
    import main
    client, attio, store = app
    monkeypatch.setattr(main, "_sync_note_to_people", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("attio")))
    assert post(client).status_code == 500  # existing behaviour
    assert count(store, "fr_jobs") == 1


def test_attio_summary_filter_does_not_gate_routing(app):
    client, attio, store = app
    body = json.dumps(dict(PAYLOAD, default_summary=None)).encode()
    assert post(client, body=body).json == {"ok": True}
    assert attio == [] and count(store, "fr_jobs") == 1


def test_disabled_routing_touches_nothing(app):
    client, attio, store = app
    runtime.configure(settings=replace(make_settings(), enabled=False), store=store)
    post(client)
    assert count(store, "fr_jobs") == 0 and count(store, "fr_webhook_deliveries") == 0 and len(attio) == 1


def test_stored_snapshot_has_no_transcript_or_summary_text(app):
    client, attio, store = app
    post(client)
    snap = json.dumps(store.get_source_snapshot("fathom", "555"))
    assert "Something" not in snap and "Please file" not in snap


def test_last_webhook_and_admin_endpoints_require_token(app):
    client, attio, store = app
    post(client)
    assert client.get("/last-webhook").status_code == 404
    assert client.get("/last-webhook", headers={"Authorization": "Bearer nope"}).status_code == 404
    assert client.get("/last-webhook", headers={"Authorization": "Bearer admintoken"}).status_code == 200
    assert client.get("/admin/folder-routing/status").status_code == 401
    assert client.get("/admin/folder-routing/status?token=admintoken").status_code == 401
    ok = client.get("/admin/folder-routing/status", headers={"Authorization": "Bearer admintoken"})
    assert ok.status_code == 200 and ok.json["fathom_writes_supported"] is False
    runtime.configure(settings=replace(make_settings(), admin_token=""), store=store)
    assert client.get("/admin/folder-routing/status").status_code == 404
    assert client.get("/last-webhook").status_code == 404


def test_review_queue_admin_flow(app):
    from folder_routing.pipeline import Pipeline
    client, attio, store = app
    post(client)
    Pipeline(runtime.settings(), store).process("fathom", "555")
    auth = {"Authorization": "Bearer admintoken"}
    items = client.get("/admin/folder-routing/review", headers=auth).json["items"]
    assert len(items) == 1 and items[0]["link"] == PAYLOAD["url"]
    r = client.post(f"/admin/folder-routing/review/{items[0]['id']}", headers=auth,
                    json={"status": "filed_manually", "note": "added in Fathom UI"})
    assert r.status_code == 200
    assert client.get("/admin/folder-routing/review", headers=auth).json["items"] == []


def _granola_transport(req):
    if req.url.path == "/v1/notes":
        return httpx.Response(200, json={"notes": [{"id": "not_P000000000001", "title": "T"}],
                                         "hasMore": False, "cursor": None})
    return httpx.Response(200, json={"id": "not_P000000000001", "summary_markdown": "s",
                                     "attendees": [{"email": "jsuiter@plsaengineering.com"}], "web_url": "u"})


def test_granola_poll_enqueues_before_attio_filters(app, monkeypatch):
    import granola_sync
    client, attio, store = app
    orig_client = httpx.Client
    monkeypatch.setattr(granola_sync.httpx, "Client",
                        lambda **kw: orig_client(transport=httpx.MockTransport(_granola_transport), **kw))
    called = []
    monkeypatch.setattr(granola_sync, "_sync_note_to_people", lambda *a, **k: called.append(a) or ["J"])
    assert granola_sync.sync() == (1, 0)
    assert count(store, "fr_jobs") == 1 and len(called) == 1


def test_explicit_backfill_window_never_routes(app, monkeypatch):
    import granola_sync
    client, attio, store = app
    orig_client = httpx.Client
    monkeypatch.setattr(granola_sync.httpx, "Client",
                        lambda **kw: orig_client(transport=httpx.MockTransport(_granola_transport), **kw))
    monkeypatch.setattr(granola_sync, "_sync_note_to_people", lambda *a, **k: ["J"])
    granola_sync.sync(created_after="2020-01-01T00:00:00Z")
    assert count(store, "fr_jobs") == 0
