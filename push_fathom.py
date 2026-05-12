import os, httpx
from collections import defaultdict
from main import _get_or_create_company, _post_note, MY_DOMAIN

ATTIO_KEY = os.environ["ATTIO_API_KEY"]
ATTIO = {"Authorization": f"Bearer {ATTIO_KEY}", "Content-Type": "application/json"}
FATHOM_KEY = os.environ['FATHOM_API_KEY']

# Cache: company_id -> set of existing note titles (fetched once per company)
_note_title_cache = defaultdict(set)


def get_existing_titles(company_id):
    if company_id in _note_title_cache:
        return _note_title_cache[company_id]
    notes, offset = [], 0
    with httpx.Client(timeout=30) as c:
        while True:
            r = c.get("https://api.attio.com/v2/notes", headers=ATTIO,
                      params={"parent_object": "companies", "parent_record_id": company_id,
                              "limit": 50, "offset": offset})
            batch = r.json().get("data", [])
            notes.extend(batch)
            if len(batch) < 50:
                break
            offset += 50
    titles = {n.get("title", "") for n in notes}
    _note_title_cache[company_id] = titles
    return titles


cursor, done, skipped = None, 0, 0
with httpx.Client(headers={"X-Api-Key": FATHOM_KEY}, timeout=30) as c:
    while True:
        params = {"include_summary": "true", "limit": 50}
        if cursor:
            params["cursor"] = cursor
        resp = c.get("https://api.fathom.ai/external/v1/meetings", params=params)
        if resp.status_code != 200:
            print(f"Fathom API error {resp.status_code}: {resp.text}")
            break
        data = resp.json()
        for m in data.get("items", []):
            title = m.get("meeting_title", "Meeting")
            domain = None
            for inv in m.get("calendar_invitees", []):
                d = inv.get("email_domain", "").lower()
                if not d:
                    email = inv.get("email", "")
                    if "@" in email:
                        d = email.split("@")[1].lower()
                if d and MY_DOMAIN not in d:
                    domain = d
                    break
            if not domain:
                print(f"  skip (no external): {title}")
                skipped += 1
                continue
            summary = (m.get("default_summary") or {}).get("markdown_formatted", "").strip()
            if not summary:
                print(f"  skip (no summary): {title}")
                skipped += 1
                continue
            url = m.get("url", "")
            cid = _get_or_create_company(domain)
            attio_title = f"Fathom: {title}"
            existing = get_existing_titles(cid)
            if attio_title in existing:
                print(f"  skip (exists): {title}")
                skipped += 1
                continue
            _post_note(cid, title, summary, url, source="Fathom")
            _note_title_cache[cid].add(attio_title)  # update cache immediately
            print(f"  CREATED: {title} -> {domain}")
            done += 1
        cursor = data.get("next_cursor") or data.get("next_page_cursor")
        if not cursor:
            break

print(f"\nDone: {done} created, {skipped} skipped.")
