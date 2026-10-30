import random
import time

import httpx

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ApiError(Exception):
    """Carries an error code safe to log (no URLs, bodies or credentials)."""

    def __init__(self, code, status=None, retryable=False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


def _retry_after(resp):
    try:
        return min(float(resp.headers.get("retry-after", "")), 60.0)
    except ValueError:
        return None


def request(client, method, url, *, max_attempts=4, base_delay=1.0, sleep=time.sleep, **kwargs):
    """Send with exponential backoff and jitter on 429/5xx/network errors.

    Only for reads and for writes the provider documents as idempotent.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt == max_attempts:
                raise ApiError(f"network:{type(e).__name__}", retryable=True) from None
            sleep(base_delay * 2 ** (attempt - 1) * (1 + random.random() / 2))
            continue
        if resp.status_code < 300:
            return resp
        if resp.status_code in RETRY_STATUS and attempt < max_attempts:
            sleep(_retry_after(resp) or base_delay * 2 ** (attempt - 1) * (1 + random.random() / 2))
            continue
        raise ApiError(f"http_{resp.status_code}", resp.status_code, resp.status_code in RETRY_STATUS)
    raise ApiError("exhausted", retryable=True)
