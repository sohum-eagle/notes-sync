import os, hmac, hashlib, httpx
from flask import Flask, request, jsonify, abort
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)


def _run_granola_sync():
    if not os.environ.get("GRANOLA_API_KEY"):
        return
    try:
        from granola_sync import sync
        sync()
    except Exception as e:
        print(f"Granola sync error: {e}")


scheduler = BackgroundScheduler()
scheduler.add_job(_run_granola_sync, "interval", minutes=15)
scheduler.start()

ATTIO_KEY      = os.environ["ATTIO_API_KEY"]
MY_DOMAIN      = os.environ.get("MY_DOMAIN", "eagleeng.com")
WEBHOOK_SECRET = os.environ.get("FATHOM_WEBHOOK_SECRET", "")
ATTIO          = {"Authorization": f"Bearer {ATTIO_KEY}", "Content-Type": "application/json"}


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        sig      = request.headers.get("x-fathom-signature", "")
        expected = hmac.new(WEBHOOK_SECRET.encode(), request.data, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            abort(401)

    payload = request.json or {}
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
    _post_note(company_id, title, summary, url)
    return jsonify(ok=True)


def _external_domain(payload):
    for inv in payload.get("calendar_invitees", []):
        d = inv.get("email_domain", "")
        if inv.get("is_external") and MY_DOMAIN not in d:
            return d.lower()
    return None


def _get_or_create_company(domain):
    r = httpx.post(
        "https://api.attio.com/v2/objects/companies/records/query",
        headers=ATTIO,
        json={"filter": {"domains": {"domain": {"$eq": domain}}}, "limit": 1},
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
    )
    print(f"{source} note created on {company_id}: {title}")


def _attio_note_exists(company_id, title):
    r = httpx.get(
        "https://api.attio.com/v2/notes",
        headers=ATTIO,
        params={"parent_object": "companies", "parent_record_id": company_id, "limit": 500},
    )
    return any(n.get("title") == title for n in r.json().get("data", []))


@app.route("/granola-sync", methods=["POST"])
def granola_sync_route():
    from granola_sync import sync
    done, skipped = sync()
    return jsonify(done=done, skipped=skipped)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
