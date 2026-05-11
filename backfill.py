import os, httpx
from main import _get_or_create_company, _post_note

FATHOM_KEY = os.environ["FATHOM_API_KEY"]
MY_DOMAIN  = os.environ.get("MY_DOMAIN", "eagleeng.com")


def _domain_from_meeting(m):
    # Try full invitee list (present in webhook payloads)
    for inv in m.get("calendar_invitees", []):
        d = inv.get("email_domain", "")
        if inv.get("is_external") and MY_DOMAIN not in d:
            return d.lower()

    # Fallback: use CRM match domain if Fathom found one (list of email strings)
    for match in m.get("crm_matches", []) or []:
        if isinstance(match, str) and "@" in match:
            d = match.split("@")[-1].lower()
            if MY_DOMAIN not in d:
                return d
        elif isinstance(match, dict):
            d = (match.get("domain") or match.get("email_domain", "")).lower()
            if d and MY_DOMAIN not in d:
                return d

    return None


def run():
    cursor, done, skipped = None, 0, 0
    with httpx.Client(headers={"X-Api-Key": FATHOM_KEY}, timeout=30) as c:
        while True:
            params = {"include_summary": "true", "include_crm_matches": "true", "limit": 50}
            if cursor:
                params["cursor"] = cursor

            resp = c.get("https://api.fathom.ai/external/v1/meetings", params=params)
            if resp.status_code != 200:
                print(f"Fathom API error {resp.status_code}: {resp.text}")
                return
            data = resp.json()

            meetings = data.get("items", [])
            print(f"Fetched {len(meetings)} meetings...")

            for m in meetings:
                title   = m.get("meeting_title", "Meeting")
                summary = (m.get("default_summary") or {}).get("markdown_formatted", "").strip()
                url     = m.get("url", "")
                domain  = _domain_from_meeting(m)

                if not summary:
                    print(f"  skip (no summary): {title}")
                    skipped += 1
                    continue
                if not domain:
                    dtype = m.get("calendar_invitees_domains_type", "unknown")
                    print(f"  skip (no external domain, type={dtype}): {title}")
                    skipped += 1
                    continue

                cid = _get_or_create_company(domain)
                _post_note(cid, title, summary, url)
                done += 1

            cursor = data.get("next_cursor") or data.get("next_page_cursor")
            if not cursor:
                break

    print(f"\nDone: {done} notes created, {skipped} skipped.")


if __name__ == "__main__":
    run()
