import json
import threading

import httpx
import pytest
from cryptography.fernet import Fernet

from folder_routing.providers.oauth import PROVIDER, DbTokenProvider, TokenUnavailable

KEY = Fernet.generate_key().decode()


def seed(store, expires_at):
    tok = {"access_token": "old-access", "refresh_token": "rt-1", "expires_at": expires_at, "client_id": "c",
           "token_endpoint": "https://auth.test/token", "resource": "https://mcp.test/mcp"}
    store.save_token(PROVIDER, Fernet(KEY.encode()).encrypt(json.dumps(tok).encode()).decode())


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
    p = DbTokenProvider(store, KEY, client=refresher(calls), clock=lambda: 1000)
    assert p.access_token() == "new-access-1"
    assert calls[0]["refresh_token"] == "rt-1" and calls[0]["grant_type"] == "refresh_token"
    stored = store.load_token(PROVIDER)
    assert "new-access" not in stored and "rt-2" not in stored
    assert DbTokenProvider(store, KEY, clock=lambda: 1000).access_token() == "new-access-1"


def test_concurrent_workers_spend_a_rotating_refresh_token_once(store):
    seed(store, expires_at=0)
    calls, results = [], []
    providers = [DbTokenProvider(store, KEY, client=refresher(calls), clock=lambda: 1000) for _ in range(6)]
    threads = [threading.Thread(target=lambda p=p: results.append(p.access_token())) for p in providers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1 and set(results) == {"new-access-1"}


def test_missing_or_undecryptable_token_is_token_unavailable(store):
    with pytest.raises(TokenUnavailable):
        DbTokenProvider(store, KEY).access_token()
    seed(store, expires_at=10 ** 10)
    with pytest.raises(TokenUnavailable):
        DbTokenProvider(store, Fernet.generate_key().decode()).access_token()
