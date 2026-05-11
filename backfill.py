import os, httpx
from main import _external_domain, _get_or_create_company, _post_note

FATHOM_KEY = os.environ["FATHOM_API_KEY"]


def run():
    cursor, done, skipped = None, 0, 0
    with httpx.Client(headers={"X-Api-Key": FATHOM_KEY}, timeout=30) as c:
        while True:
            params = {"include_summary": "true", "limit": 50}
            if cursor:
                params["cursor"] = cursor
            data = c.get("https://api.fathom.ai/v1/meetings", params=params).json()

            for m in data.get("data", []):
                title   = m.get("meeting_title", "Meeting")
                summary = (m.get("default_summary") or {}).get("markdown_formatted", "").strip()
                url     = m.get("url") or m.get("share_url", "")
                domain  = _external_domain(m)

                if not summary or not domain:
                    skipped += 1
                    continue

                cid = _get_or_create_company(domain)
                _post_note(cid, title, summary, url)
                done += 1

            cursor = data.get("next_cursor")
            if not cursor:
                break

    print(f"\nDone: {done} notes created, {skipped} skipped.")


if __name__ == "__main__":
    run()
