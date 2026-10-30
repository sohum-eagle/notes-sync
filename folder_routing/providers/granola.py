"""Granola adapters.

Reads use the public REST API (https://docs.granola.ai/api-reference/openapi.json),
whose IDs are not_... (notes) and fol_... (folders). The REST API has no
folder-write endpoint. Writes use Granola MCP's add_note_to_folder, which takes
a note's not_ ID or UUID and the folder's UUID, needs a user OAuth token, and
only works for notes that user owns or collaborates on. MCP rejects API keys.
"""
import re

import httpx

from .. import http
from . import FolderInfo, WriteRejected, WriteResult
from .mcp_client import McpError, tool_payload

BASE = "https://public-api.granola.ai"

MEETING_ID_RE = re.compile(r'<meeting id="([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"')
PAGINATION_RE = re.compile(r"<pagination\b[^>]*>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

REJECT_HINTS = ("permission", "not allowed", "forbidden", "only notes", "viewer", "read-only",
                "read only", "not found", "cannot add", "can't add", "access")


class GranolaReader:
    def __init__(self, api_key, client=None, sleep=None):
        self.client = client or httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        self.kw = {"sleep": sleep} if sleep else {}

    def _get(self, path, params=None):
        return http.request(self.client, "GET", f"{BASE}{path}", params=params or {}, **self.kw).json()

    def iter_notes(self, updated_after=None, folder_id=None, page_size=30, max_pages=500):
        cursor, pages = None, 0
        while pages < max_pages:
            params = {"page_size": page_size}
            if updated_after:
                params["updated_after"] = updated_after
            if folder_id:
                params["folder_id"] = folder_id
            if cursor:
                params["cursor"] = cursor
            data = self._get("/v1/notes", params)
            yield from data.get("notes") or []
            pages += 1
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor:
                return

    def iter_transcript(self, note_id, max_pages=100):
        cursor, pages = None, 0
        while pages < max_pages:
            params = {"page_size": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._get(f"/v1/notes/{note_id}/transcript", params)
            yield from data.get("transcript") or []
            pages += 1
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor:
                return

    def get_note(self, note_id, include_transcript=True):
        """Returns (note, transcript_items) or (None, None) if deleted or no longer accessible."""
        try:
            params = {"include": "transcript"} if include_transcript else {}
            note = self._get(f"/v1/notes/{note_id}", params)
            return note, note.get("transcript")
        except http.ApiError as e:
            if e.status == 404:
                return None, None
            if e.status != 413 or not include_transcript:
                raise
        # 413 TRANSCRIPT_TOO_LARGE: documented fallback is the paginated transcript endpoint.
        note = self._get(f"/v1/notes/{note_id}")
        return note, list(self.iter_transcript(note_id))

    def folder_ids_of(self, note_id):
        note, _ = self.get_note(note_id, include_transcript=False)
        if note is None:
            return None
        return {str(f.get("id")).lower() for f in note.get("folder_membership") or [] if f.get("id")}

    def iter_folders(self, max_pages=50):
        cursor, pages = None, 0
        while pages < max_pages:
            params = {"page_size": 100}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/v1/folders", params)
            yield from data.get("folders") or []
            pages += 1
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor:
                return

    def list_webhook_endpoints(self):
        return self._get("/v1/webhook-endpoints").get("webhook_endpoints") or []


class GranolaMcpWriter:
    supported = True

    def __init__(self, mcp):
        self.mcp = mcp

    def get_folder(self, folder_uuid):
        offset = 0
        for _ in range(20):
            data = tool_payload(self.mcp.call_tool("list_meeting_folders", {"limit": 100, "offset": offset}))
            for f in data.get("folders") or []:
                if str(f.get("id")) == folder_uuid:
                    return FolderInfo(
                        folder_id=str(f.get("id")), title=str(f.get("title") or ""),
                        visibility=str(f.get("visibility") or ""), audience=str(f.get("audience") or ""),
                        parent_folder_id=str(f.get("parent_folder_id") or ""), role=str(f.get("role") or ""))
            if not data.get("truncated") or data.get("next_offset") is None:
                return None
            offset = int(data["next_offset"])
        return None

    def list_folder_note_uuids(self, folder_uuid, from_date, to_date):
        """Read-only. Note UUIDs in a folder, used to verify the REST fol_ <-> UUID folder mapping.
        Granola MCP returns meetings as XML text; only IDs and pagination attributes are parsed."""
        uuids, cursor = set(), None
        for _ in range(100):
            args = {"folder_id": folder_uuid, "time_range": "custom", "from_date": from_date,
                    "to_date": to_date, "limit": 200, "max_participants": 0}
            if cursor:
                args["cursor"] = cursor
            result = self.mcp.call_tool("list_meetings", args)
            text = "\n".join(b.get("text") or "" for b in result.get("content") or [] if b.get("type") == "text")
            uuids |= set(MEETING_ID_RE.findall(text))
            tag = PAGINATION_RE.search(text)
            attrs = dict(ATTR_RE.findall(tag.group(0))) if tag else {}
            cursor = attrs.get("next_cursor")
            if attrs.get("has_more") != "true" or not cursor:
                return uuids
        raise RuntimeError("folder listing did not finish")

    def add(self, note_id, folder_uuid):
        """Additive and idempotent per Granola's tool contract. Raises WriteRejected / WriteUncertain."""
        try:
            result = self.mcp.call_tool("add_note_to_folder", {"note_id": note_id, "folder_id": folder_uuid},
                                        is_write=True)
        except McpError as e:
            if e.retryable:
                raise
            raise WriteRejected(e.code) from None
        payload = tool_payload(result)
        if result.get("isError"):
            text = str(payload).lower()
            raise WriteRejected("mcp_tool_refused" if any(h in text for h in REJECT_HINTS) else "mcp_tool_error")
        text = str(payload).lower()
        if "already_in_folder" in text:
            outcome = "already_in_folder"
        elif "added" in text:
            outcome = "added"
        else:
            outcome = "unknown"  # response shape not yet observed in production; membership is verified anyway
        return WriteResult(outcome=outcome)
