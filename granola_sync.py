import os, httpx
from datetime import datetime, timedelta, timezone
from main import _sync_note_to_people, MY_DOMAIN

# Lock TTL must exceed the lookback window so a note caught by two
# consecutive cron runs (interval 15 min, lookback 20 min) is posted once.
DEDUP_LOCK_TTL = 1800  # 30 minutes

GRANOLA_KEY = os.environ["GRANOLA_API_KEY"]
GRANOLA     = {"Authorization": f"Bearer {GRANOLA_KEY}"}
BASE        = "https://public-api.granola.ai"
LOOKBACK    = int(os.environ.get("GRANOLA_LOOKBACK_MINUTES", "20"))


def sync(created_after=None):
    """Sync Granola notes to Attio. created_after is an ISO datetime string."""
    if created_after is None:
        created_after = (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK)).strftime("%Y-%m-%dT%H:%M:%SZ")

    cursor = None
    done = skipped = 0

    with httpx.Client(headers=GRANOLA, timeout=30) as c:
        while True:
            params = {"updated_after": created_after, "page_size": 30}
            if cursor:
                params["cursor"] = cursor

            resp = c.get(f"{BASE}/v1/notes", params=params)
            if resp.status_code != 200:
                print(f"Granola API error {resp.status_code}: {resp.text}")
                return done, skipped

            data = resp.json()
            for note in data.get("notes", []):
                d, s = _process_note(c, note)
                done += d
                skipped += s

            if not data.get("hasMore"):
                break
            cursor = data.get("cursor")

    print(f"Granola sync done: {done} created, {skipped} skipped")
    return done, skipped


def _process_note(client, note):
    note_id = note.get("id", "")
    title   = (note.get("title") or "Meeting").strip()
    web_url = note.get("web_url", "")

    # Fetch full note for summary_markdown (list endpoint omits it)
    full_resp = client.get(f"{BASE}/v1/notes/{note_id}")
    if full_resp.status_code != 200:
        return 0, 1
    full = full_resp.json()

    summary = (full.get("summary_markdown") or "").strip()
    if not summary:
        print(f"  skip (no summary): {title}")
        return 0, 1

    attendees = full.get("attendees") or note.get("attendees") or []
    # Attach to each external attendee's Attio Person record (find-or-create).
    # 30-min dedup lock so a note caught by two consecutive cron runs posts once.
    posted = _sync_note_to_people(attendees, title, summary, web_url,
                                  source="Granola", dedup_ttl=DEDUP_LOCK_TTL)
    if not posted:
        print(f"  skip (no external attendee): {title}")
        return 0, 1
    return 1, 0


if __name__ == "__main__":
    sync()
