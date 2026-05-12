import os, httpx
from datetime import datetime, timedelta, timezone
from main import _get_or_create_company, _post_note, _attio_note_exists, MY_DOMAIN

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
    domain = _external_domain(attendees)
    if not domain:
        print(f"  skip (no external domain): {title}")
        return 0, 1

    company_id = _get_or_create_company(domain)

    attio_title = f"Granola: {title}"
    if _attio_note_exists(company_id, attio_title):
        print(f"  skip (already synced): {title}")
        return 0, 1

    _post_note(company_id, title, summary, web_url, source="Granola")
    return 1, 0


def _external_domain(attendees):
    for att in attendees:
        email = att.get("email", "")
        if "@" in email:
            domain = email.split("@")[1].lower()
            if MY_DOMAIN not in domain:
                return domain
    return None


if __name__ == "__main__":
    sync()
