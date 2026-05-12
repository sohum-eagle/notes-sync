import os, hmac, hashlib, sys
from datetime import datetime
print("notes-sync starting up...", flush=True)

try:
    import httpx
    from flask import Flask, request, jsonify, abort
    print("imports OK", flush=True)
except Exception as _e:
    print(f"IMPORT ERROR: {_e}", flush=True)
    sys.exit(1)

app = Flask(__name__)
_last_webhook = {}  # stores last received payload for debugging

ATTIO_KEY      = os.environ.get("ATTIO_API_KEY", "")
MY_DOMAIN      = os.environ.get("MY_DOMAIN", "eagleeng.com")
WEBHOOK_SECRET = os.environ.get("FATHOM_WEBHOOK_SECRET", "")
ATTIO          = {"Authorization": f"Bearer {ATTIO_KEY}", "Content-Type": "application/json"}

print(f"env OK — ATTIO_KEY={'set' if ATTIO_KEY else 'MISSING'}", flush=True)


@app.route("/ping")
def ping():
    return "pong"


@app.route("/last-webhook")
def last_webhook():
    return jsonify(_last_webhook)


@app.route("/webhook", methods=["POST"])
def webhook():
    # Capture raw data and payload BEFORE any auth check so /last-webhook always shows what arrived
    raw_body = request.data
    payload = request.json or {}
    global _last_webhook
    _last_webhook = payload
    all_headers = dict(request.headers)
    print(f"WEBHOOK HIT: title={payload.get('meeting_title')} sig_header={request.headers.get('x-fathom-signature','NONE')}", flush=True)

    if WEBHOOK_SECRET:
        sig = request.headers.get("x-fathom-signature", "")
        if sig:
            sig_value = sig.replace("sha256=", "").replace("v1=", "")
            expected = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig_value, expected):
                print(f"WEBHOOK HMAC FAILED: got={sig_value[:20]}... expected={expected[:20]}...", flush=True)
                abort(401)
    title   = payload.get("meeting_title", "Meeting")
    summary = (payload.get("default_summary") or {}).get("markdown_formatted", "").strip()
    url     = payload.get("url") or payload.get("share_url", "")

    if not summary:
        return jsonify(ok=True)

    domain = _external_domain(payload)
    if not domain:
        print(f"skip (no external attendee): {title}")
        return jsonify(ok=True)

    company_id = _get_or_create_company(domain)
    attio_title = f"Fathom: {title}"
    if _attio_note_exists(company_id, attio_title):
        print(f"skip (already in Attio): {title}")
        return jsonify(ok=True)
    _post_note(company_id, title, summary, url)
    return jsonify(ok=True)


def _external_domain(payload):
    for inv in payload.get("calendar_invitees", []):
        d = inv.get("email_domain", "")
        if not d:
            email = inv.get("email", "")
            if "@" in email:
                d = email.split("@")[1]
        if d and MY_DOMAIN not in d.lower():
            return d.lower()
    return None


def _get_or_create_company(domain):
    r = httpx.post(
        "https://api.attio.com/v2/objects/companies/records/query",
        headers=ATTIO,
        json={"filter": {"domains": {"domain": {"$eq": domain}}}, "limit": 1},
        timeout=30,
    )
    rows = r.json().get("data", [])
    if rows:
        return rows[0]["id"]["record_id"]

    r = httpx.post(
        "https://api.attio.com/v2/objects/companies/records",
        headers=ATTIO,
        json={"data": {"values": {
            "domains": [{"domain": domain}],
            "name":    [{"value": domain.split(".")[0].title()}],
        }}},
        timeout=30,
    )
    record_id = r.json()["data"]["id"]["record_id"]
    print(f"Created Attio company: {domain} → {record_id}")
    return record_id


def _post_note(company_id, title, summary, url, source="Fathom"):
    link_label = f"View {source} notes"
    content = summary + (f"\n\n[{link_label}]({url})" if url else "")
    httpx.post(
        "https://api.attio.com/v2/notes",
        headers=ATTIO,
        json={"data": {
            "parent_object":    "companies",
            "parent_record_id": company_id,
            "title":   f"{source}: {title}",
            "format":  "markdown",
            "content": content,
        }},
        timeout=30,
    )
    print(f"{source} note created on {company_id}: {title}".encode('ascii', 'replace').decode('ascii'))


def _attio_note_exists(company_id, title):
    r = httpx.get(
        "https://api.attio.com/v2/notes",
        headers=ATTIO,
        params={"parent_object": "companies", "parent_record_id": company_id, "limit": 500},
        timeout=30,
    )
    return any(n.get("title") == title for n in r.json().get("data", []))


@app.route("/latest-email")
def latest_email():
    return _interaction_endpoint("last_email_interaction")


@app.route("/last-meeting")
def last_meeting():
    return _interaction_endpoint("last_calendar_interaction")


@app.route("/summary-notes")
def summary_notes():
    domain = request.args.get("domain", "").strip().lower()
    if not domain:
        return jsonify(summary="No notes")
    company_id = _find_company(domain)
    if not company_id:
        return jsonify(summary="No notes")
    notes = _get_all_notes(company_id)
    if not notes:
        return jsonify(summary="No notes")
    return jsonify(summary=_summarize(notes))


def _interaction_endpoint(slug):
    domain = request.args.get("domain", "").strip().lower()
    if not domain:
        return jsonify(date=None)
    company_id = _find_company(domain)
    if not company_id:
        return jsonify(date=None)
    r = httpx.get(
        f"https://api.attio.com/v2/objects/companies/records/{company_id}",
        headers=ATTIO,
        timeout=30,
    )
    entries = r.json().get("data", {}).get("values", {}).get(slug, [])
    if not entries:
        return jsonify(date=None)
    return jsonify(date=_fmt_date(entries[0].get("interacted_at")))


def _fmt_date(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str[:19])
        return dt.strftime("%-m/%-d/%y")
    except Exception:
        return iso_str


def _find_company(domain):
    r = httpx.post(
        "https://api.attio.com/v2/objects/companies/records/query",
        headers=ATTIO,
        json={"filter": {"domains": {"domain": {"$eq": domain}}}, "limit": 1},
        timeout=30,
    )
    rows = r.json().get("data", [])
    return rows[0]["id"]["record_id"] if rows else None


def _get_all_notes(company_id):
    notes, offset = [], 0
    while True:
        r = httpx.get(
            "https://api.attio.com/v2/notes",
            headers=ATTIO,
            params={"parent_object": "companies", "parent_record_id": company_id,
                    "limit": 50, "offset": offset},
            timeout=30,
        )
        batch = r.json().get("data", [])
        notes.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
    return notes


def _summarize(notes):
    import anthropic
    text = "\n\n---\n\n".join(
        f"Title: {n.get('title','')}\n{n.get('content_plaintext') or ''}"
        for n in notes
    ).strip()
    if not text:
        return "No notes"
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": (
            "Summarize these meeting notes in exactly 3 bullet points using this format:\n"
            "• [What the company does — 1 sentence]\n"
            "• [Next steps — 1 sentence]\n"
            "• [Any other important detail — 1 sentence, or leave blank if nothing important]\n\n"
            "Return only the 3 bullet points, no other text.\n\n"
            f"Notes:\n{text[:8000]}"
        )}],
    )
    return resp.content[0].text




@app.route("/granola-sync", methods=["POST"])
def granola_sync_route():
    from granola_sync import sync
    done, skipped = sync()
    return jsonify(done=done, skipped=skipped)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
