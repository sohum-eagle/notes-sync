import json
import threading

import httpx
import pytest
from cryptography.fernet import Fernet

from folder_routing.providers.oauth import DbTokenProvider, TokenUnavailable, confirm_account, token_key

KEY = Fernet.generate_key().decode()
ACCOUNT = "sohum@eagleeng.com"


def seed(store, expires_at):
    tok = {"access_token": "old-access", "refresh_token": "rt-1", "expires_at": expires_at, "client_id": "c",
           "token_endpoint": "https://auth.test/token", "resource": "https://mcp.test/mcp"}
    store.save_token(token_key(ACCOUNT), Fernet(KEY.encode()).encrypt(json.dumps(tok).encode()).decode())


def refresher(calls):
    lock = threading.Lock()

    def handler(req):
        with lock:
            calls.append(dict(x.split("=") for x in req.content.decode().split("&")))
            n = len(calls)
        return httpx.Response(200, json={"access_token": f"new-access-{n}", "refresh_token": f"rt-{n + 1}",
                                         "expires_in": 3600})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_tokens_are_encrypted_and_rotation_is_persisted(store):
    seed(store, expires_at=0)
    calls = []
    p = DbTokenProvider(store, KEY, ACCOUNT, client=refresher(calls), clock=lambda: 1000)
    assert p.access_token() == "new-access-1"
    assert calls[0]["refresh_token"] == "rt-1" and calls[0]["grant_type"] == "refresh_token"
    stored = store.load_token(token_key(ACCOUNT))
    assert "new-access" not in stored and "rt-2" not in stored
    assert DbTokenProvider(store, KEY, ACCOUNT, clock=lambda: 1000).access_token() == "new-access-1"


def test_concurrent_workers_spend_a_rotating_refresh_token_once(store):
    seed(store, expires_at=0)
    calls, results = [], []
    providers = [DbTokenProvider(store, KEY, ACCOUNT, client=refresher(calls), clock=lambda: 1000) for _ in range(6)]
    threads = [threading.Thread(target=lambda p=p: results.append(p.access_token())) for p in providers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1 and set(results) == {"new-access-1"}


def test_missing_or_undecryptable_token_is_token_unavailable(store):
    with pytest.raises(TokenUnavailable):
        DbTokenProvider(store, KEY, ACCOUNT).access_token()
    seed(store, expires_at=10 ** 10)
    with pytest.raises(TokenUnavailable):
        DbTokenProvider(store, Fernet.generate_key().decode(), ACCOUNT).access_token()


def test_tokens_are_isolated_per_owner(store):
    seed(store, expires_at=10 ** 10)
    with pytest.raises(TokenUnavailable):
        DbTokenProvider(store, KEY, "danny@eagleeng.com").access_token()


@pytest.mark.parametrize("email,ok", [("Sohum@EagleEng.com", True), ("danny@eagleeng.com", False)])
def test_login_is_refused_unless_granola_reports_the_named_account(email, ok):
    def handler(req):
        msg = json.loads(req.content)
        if "id" not in msg:
            return httpx.Response(202)
        result = {} if msg["method"] == "initialize" else {
            "content": [{"type": "text", "text": json.dumps({"email": email})}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    if ok:
        confirm_account("https://mcp.test/mcp", "tok", ACCOUNT, client)
    else:
        with pytest.raises(TokenUnavailable):
            confirm_account("https://mcp.test/mcp", "tok", ACCOUNT, client)
