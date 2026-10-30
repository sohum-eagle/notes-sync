"""Standard Webhooks verification, as documented by both Fathom
(developers.fathom.ai/webhooks#verifying-webhooks) and Granola (docs.granola.ai/webhooks)."""
import base64
import binascii
import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300


def _header(headers, name):
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


def delivery_id(headers):
    return _header(headers, "webhook-id")


def verify(secret, headers, raw_body, tolerance=TOLERANCE_SECONDS, now=None):
    """True only for a fresh, correctly signed delivery. Never raises."""
    if not secret:
        return False
    msg_id = _header(headers, "webhook-id")
    stamp = _header(headers, "webhook-timestamp")
    sigs = _header(headers, "webhook-signature")
    if not (msg_id and stamp and sigs):
        return False
    try:
        ts = int(stamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        return False
    try:
        key = base64.b64decode(secret[len("whsec_"):] if secret.startswith("whsec_") else secret, validate=True)
    except (binascii.Error, ValueError):
        return False
    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    signed = f"{msg_id}.{stamp}.".encode() + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in sigs.split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False
