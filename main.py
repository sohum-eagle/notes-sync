import os, httpx
from flask import Flask, request, jsonify

app = Flask(__name__)

ATTIO_KEY = os.environ["ATTIO_API_KEY"]
MY_DOMAIN  = os.environ.get("MY_DOMAIN", "eagleeng.com")
ATTIO      = {"Authorization": f"Bearer {ATTIO_KEY}", "Content-Type": "application/json"}


@app.route("/webhook", methods=["POST"])
def webhook():
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


def _post_note(company_id, title, summary, url):
    content = summary + (f"\n\n[View Fathom notes]({url})" if url else "")
    httpx.post(
        "https://api.attio.com/v2/notes",
        headers=ATTIO,
        json={"data": {
            "parent_object":    "companies",
            "parent_record_id": company_id,
            "title":   f"Fathom: {title}",
            "format":  "markdown",
            "content": content,
        }},
    )
    print(f"Note created on {company_id}: {title}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
