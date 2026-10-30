import time

from folder_routing import signatures
from tests.fakes import SECRET, sign

BODY = b'{"recording_id": 1}'


def test_valid_signature():
    assert signatures.verify(SECRET, sign(BODY), BODY)


def test_header_names_are_case_insensitive():
    headers = {k.title(): v for k, v in sign(BODY).items()}
    assert signatures.verify(SECRET, headers, BODY)


def test_tampered_body_rejected():
    assert not signatures.verify(SECRET, sign(BODY), b'{"recording_id": 2}')


def test_wrong_secret_rejected():
    other = "whsec_" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    assert not signatures.verify(other, sign(BODY), BODY)


def test_stale_timestamp_rejected():
    assert not signatures.verify(SECRET, sign(BODY, ts=time.time() - 3600), BODY)


def test_missing_headers_or_secret_rejected():
    assert not signatures.verify(SECRET, {}, BODY)
    assert not signatures.verify("", sign(BODY), BODY)
    assert not signatures.verify(SECRET, {"x-fathom-signature": "abc"}, BODY)


def test_any_of_multiple_signatures_may_match():
    h = sign(BODY)
    h["webhook-signature"] = "v1,AAAA " + h["webhook-signature"]
    assert signatures.verify(SECRET, h, BODY)


def test_garbage_secret_does_not_raise():
    assert not signatures.verify("whsec_***not-base64***", sign(BODY), BODY)
