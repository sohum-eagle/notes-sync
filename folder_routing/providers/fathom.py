"""Fathom adapter. Read paths use the documented External API
(https://developers.fathom.ai/api-reference/openapi.yaml). That spec has no
folder endpoints, so folder membership and folder writes are unavailable and
the writer refuses unconditionally. No undocumented endpoints, no session cookies.
"""
import httpx

from .. import http
from . import CapabilityUnavailable

BASE = "https://api.fathom.ai/external/v1"


class FathomReader:
    def __init__(self, api_key, client=None, sleep=None):
        self.client = client or httpx.Client(headers={"X-Api-Key": api_key}, timeout=30)
        self.kw = {"sleep": sleep} if sleep else {}

    def iter_meetings(self, created_after=None, include_transcript=True, max_pages=200):
        """GET /meetings, following next_cursor until exhausted."""
        cursor, pages = None, 0
        while pages < max_pages:
            params = {"include_transcript": "true" if include_transcript else "false"}
            if created_after:
                params["created_after"] = created_after
            if cursor:
                params["cursor"] = cursor
            data = http.request(self.client, "GET", f"{BASE}/meetings", params=params, **self.kw).json()
            yield from data.get("items") or []
            pages += 1
            cursor = data.get("next_cursor")
            if not cursor:
                return

    def get_transcript(self, recording_id):
        resp = http.request(self.client, "GET", f"{BASE}/recordings/{int(recording_id)}/transcript", **self.kw)
        return (resp.json() or {}).get("transcript") or []

    def folder_membership(self, recording_id):
        raise CapabilityUnavailable("fathom_api_has_no_folder_read")


class FathomFolderWriter:
    supported = False

    def get_folder(self, folder_id):
        raise CapabilityUnavailable("fathom_api_has_no_folder_read")

    def add(self, source_id, folder_id):
        raise CapabilityUnavailable("fathom_api_has_no_folder_write")
