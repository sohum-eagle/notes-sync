import os, httpx
from collections import defaultdict

ATTIO_KEY = os.environ["ATTIO_API_KEY"]
ATTIO = {"Authorization": f"Bearer {ATTIO_KEY}", "Content-Type": "application/json"}


def get_all_companies():
    companies, cursor = [], None
    with httpx.Client(timeout=30) as c:
        while True:
            body = {"limit": 500}
            if cursor:
                body["cursor"] = cursor
            r = c.post("https://api.attio.com/v2/objects/companies/records/query",
                       headers=ATTIO, json=body)
            data = r.json()
            companies.extend(data.get("data", []))
            cursor = data.get("next_cursor")
            if not cursor:
                break
    return companies


def get_notes(company_id):
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
    return notes


def delete_note(note_id):
    r = httpx.delete(f"https://api.attio.com/v2/notes/{note_id}", headers=ATTIO, timeout=30)
    return r.status_code in (200, 204)


def run():
    print("Fetching companies...")
    companies = get_all_companies()
    print(f"{len(companies)} companies. Scanning for duplicate Fathom notes...")

    deleted = 0
    for company in companies:
        cid = company["id"]["record_id"]
        cname = (company.get("values", {}).get("name") or [{}])[0].get("value", cid)
        notes = get_notes(cid)
        if not notes:
            continue

        # Group notes by title
        by_title = defaultdict(list)
        for note in notes:
            title = note.get("title", "")
            if title.startswith("Fathom:"):
                by_title[title].append(note)

        for title, dupes in by_title.items():
            if len(dupes) < 2:
                continue
            # Keep the first one (oldest), delete the rest
            to_delete = dupes[1:]
            for note in to_delete:
                nid = note["id"]["note_id"]
                if delete_note(nid):
                    print(f"  Deleted duplicate '{title}' on {cname}")
                    deleted += 1

    print(f"\nDone. Deleted {deleted} duplicate notes.")


if __name__ == "__main__":
    run()
