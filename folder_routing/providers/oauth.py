"""Granola MCP OAuth tokens.

Granola documents browser OAuth with dynamic client registration for MCP. Its
authorization-server metadata also advertises refresh_token and offline_access,
but Granola does not document unattended server use. This module therefore:
  * runs the documented browser flow once per note owner, interactively, via the CLI
  * refuses to save a token unless Granola reports the named account
  * stores tokens only as Fernet ciphertext in Postgres, one row per account
  * refreshes under a row lock so a rotating refresh token is used exactly once
If refresh is refused, writes stop until someone logs in again.
"""
import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.parse

import httpx

PROVIDER = "granola_mcp"
RESOURCE_METADATA = "https://mcp.granola.ai/.well-known/oauth-protected-resource"


class TokenUnavailable(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fernet(key):
    if not key:
        raise TokenUnavailable("no_token_encryption_key")
    from cryptography.fernet import Fernet  # lazy: only needed once writes are configured
    return Fernet(key.encode() if isinstance(key, str) else key)


def token_key(account):
    return f"{PROVIDER}:{account.strip().lower()}"


class DbTokenProvider:
    def __init__(self, store, encryption_key, account, client=None, clock=time.time):
        self.key = token_key(account)
        self.store = store
        self.fernet = _fernet(encryption_key)
        self.client = client or httpx.Client(timeout=30)
        self.clock = clock
        self.cached = None

    def _decrypt(self, ciphertext):
        from cryptography.fernet import InvalidToken
        try:
            return json.loads(self.fernet.decrypt(ciphertext.encode()).decode())
        except InvalidToken:
            raise TokenUnavailable("token_decrypt_failed") from None

    def _encrypt(self, tok):
        return self.fernet.encrypt(json.dumps(tok).encode()).decode()

    def access_token(self):
        if self.cached is None:
            ciphertext = self.store.load_token(self.key)
            if not ciphertext:
                raise TokenUnavailable("not_logged_in")
            self.cached = self._decrypt(ciphertext)
        if self.cached.get("expires_at", 0) - 60 < self.clock():
            self.refresh()
        return self.cached["access_token"]

    def refresh(self):
        with self.store.conn() as c:
            ciphertext = self.store.load_token(self.key, for_update_conn=c)
            if not ciphertext:
                raise TokenUnavailable("not_logged_in")
            tok = self._decrypt(ciphertext)
            stale = self.cached and self.cached.get("access_token") == tok.get("access_token")
            if not stale and tok.get("expires_at", 0) - 60 > self.clock():
                self.cached = tok  # another worker already refreshed
                return
            if not tok.get("refresh_token"):
                raise TokenUnavailable("no_refresh_token")
            resp = self.client.post(tok["token_endpoint"], data={
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                "client_id": tok["client_id"], "resource": tok["resource"]})
            if resp.status_code >= 400:
                raise TokenUnavailable(f"refresh_http_{resp.status_code}")
            body = resp.json()
            tok.update(access_token=body["access_token"],
                       refresh_token=body.get("refresh_token") or tok["refresh_token"],
                       expires_at=self.clock() + int(body.get("expires_in", 3600)))
            self.store.save_token(self.key, self._encrypt(tok), conn=c)
        self.cached = tok


class _StaticToken:
    def __init__(self, token):
        self.token = token

    def access_token(self):
        return self.token

    def refresh(self):
        raise TokenUnavailable("login_token_rejected")


def confirm_account(resource, access_token, account, client=None):
    """Ask Granola which account the new token belongs to; raise unless it is `account`."""
    from .mcp_client import McpHttpClient, tool_payload
    info = tool_payload(McpHttpClient(resource, _StaticToken(access_token), client).call_tool("get_account_info", {}))
    if str(info.get("email") or "").strip().lower() != account.strip().lower():
        raise TokenUnavailable("account_mismatch")


def interactive_login(store, encryption_key, account, port=8765, scope=None, client=None, out=print):
    """One-time browser OAuth (authorization code + PKCE, loopback redirect), run by the note owner."""
    client = client or httpx.Client(timeout=30)
    fernet = _fernet(encryption_key)
    prm = client.get(RESOURCE_METADATA).json()
    issuer = prm["authorization_servers"][0]
    asm = client.get(f"{issuer}/.well-known/oauth-authorization-server").json()
    redirect = f"http://127.0.0.1:{port}/callback"
    reg = client.post(asm["registration_endpoint"], json={
        "client_name": "notes-sync folder routing",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }).json()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    scope = scope or " ".join(list(prm.get("scopes_supported") or []) + ["offline_access"])
    url = asm["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "scope": scope, "resource": prm["resource"]})
    out(f"Open this URL in a browser and sign in to Granola as {account}:\n{url}")
    code = _wait_for_code(port, state)
    body = client.post(asm["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": reg["client_id"], "code_verifier": verifier, "resource": prm["resource"]})
    if body.status_code >= 400:
        raise TokenUnavailable(f"token_http_{body.status_code}")
    body = body.json()
    confirm_account(prm["resource"], body["access_token"], account, client)
    tok = {"access_token": body["access_token"], "refresh_token": body.get("refresh_token", ""),
           "expires_at": time.time() + int(body.get("expires_in", 3600)), "client_id": reg["client_id"],
           "token_endpoint": asm["token_endpoint"], "resource": prm["resource"]}
    store.save_token(token_key(account), fernet.encrypt(json.dumps(tok).encode()).decode())
    out(f"Saved encrypted token. Refresh token issued: {'yes' if tok['refresh_token'] else 'NO (writes will stop when it expires)'}")


def _wait_for_code(port, state):
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = q.get("state", [""])[0] == state and q.get("code")
            if ok:
                result["code"] = q["code"][0]
            self.send_response(200 if ok else 400)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab." if ok else b"State mismatch.")

        def log_message(self, *args):
            pass

    with http.server.HTTPServer(("127.0.0.1", port), Handler) as srv:
        while "code" not in result:
            srv.handle_request()
    return result["code"]
