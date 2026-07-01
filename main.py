import os, hmac, hashlib, sys, time, threading
from datetime import datetime

# Simple TTL cache: key -> (value, expiry_timestamp)
_cache = {}
CACHE_TTL = 300  # 5 minutes

DEDUP_TTL = 600  # 10 minutes — window to suppress duplicate webhooks


def _claim_note(company_id, title, ttl=DEDUP_TTL):
    """
    Cross-process dedup using atomic file creation.
    Returns True if this worker should proceed with posting.
    Returns False if this note was claimed within `ttl` seconds.
    Uses open(..., 'x') which is atomic on Linux — only one process wins.
    Fathom webhooks use the default 10-min TTL (retries fire ~60s apart);
    Granola cron passes a longer TTL than its lookback window so the same
    note seen by two consecutive cron runs is only posted once.
    """
    key = hashlib.md5(f"{company_id}:{title}".encode()).hexdigest()
    path = f"/tmp/note_dedup_{key}"
    try:
        mtime = os.path.getmtime(path)
        if time.time() - mtime < ttl:
            return False  # claimed recently
        os.remove(path)   # expired — remove so we can reclaim
    except FileNotFoundError:
        pass
    try:
        with open(path, "x") as f:
            f.write(str(time.time()))
        return True   # claimed by this worker
    except FileExistsError:
        return False  # another worker beat us to it (race condition)

def _cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None

def _cache_set(key, value):
    _cache[key] = (value, time.time() + CACHE_TTL)
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
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
NOTIFY_EMAIL   = "sohum@eagleeng.com"

# Personal / free email providers — never create a "company" from these domains
# (that's the bug that dumped 63 notes onto a junk "Google" @ gmail.com record).
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "outlook.co.uk", "hotmail.com",
    "hotmail.co.uk", "live.com", "msn.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
    "pm.me", "gmx.com", "gmx.net", "zoho.com", "hey.com", "fastmail.com",
    "yandex.com", "mail.com", "qq.com", "163.com",
}

print(f"env OK — ATTIO_KEY={'set' if ATTIO_KEY else 'MISSING'}", flush=True)


def _send_notification(title, domain, source, url=""):
    """Fire-and-forget email via Resend HTTP API — called in a daemon thread."""
    if not RESEND_API_KEY:
        return
    try:
        body = f"New {source} note synced to Attio\n\nMeeting: {title}\nAttached to: {domain}"
        if url:
            body += f"\nLink: {url}"
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from":    "Notes Sync <onboarding@resend.dev>",
                "to":      [NOTIFY_EMAIL],
                "subject": f"Note synced: {title}",
                "text":    body,
            },
            timeout=15,
        )
        print(f"Notification sent ({r.status_code}) for: {title}", flush=True)
    except Exception as e:
        print(f"Email notification failed: {e}", flush=True)


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

    posted = _sync_note_to_people(payload.get("calendar_invitees", []), title, summary, url, source="Fathom")
    if not posted:
        print(f"skip (no external attendee): {title}", flush=True)
    return jsonify(ok=True, posted=len(posted))


def _post_target(parent_object, parent_id, attio_title, title, summary, url, source, dedup_ttl):
    """Post the note to one parent (person or company) with dedup. Returns True if posted."""
    if not parent_id:
        return False
    # Cross-process dedup: atomic file claim beats Attio's list-consistency lag
    if not _claim_note(f"{parent_object}:{parent_id}", attio_title, ttl=dedup_ttl):
        return False
    if _attio_note_exists(parent_object, parent_id, attio_title):
        return False
    _post_note(parent_object, parent_id, title, summary, url, source=source)
    return True


def _sync_note_to_people(invitees, title, summary, url, source, dedup_ttl=DEDUP_TTL):
    """Attach the meeting note to BOTH each external attendee's Attio Person record
    AND their company (for real corporate domains — never free-email providers).
    Returns the list of person display-names posted to. One email per meeting."""
    attendees = _external_attendees(invitees)
    attio_title = f"{source}: {title}"
    posted = []
    companies_done = set()
    for att in attendees:
        email  = att["email"]
        domain = email.split("@")[1]
        # 1) Person — always
        if _post_target("people", _find_or_create_person(email, att["name"]),
                        attio_title, title, summary, url, source, dedup_ttl):
            posted.append(att["name"])
        # 2) Company — only for real corporate domains (skip gmail/outlook/etc.)
        if domain not in FREE_EMAIL_DOMAINS:
            cid = _get_or_create_company(domain)
            if cid and cid not in companies_done:
                companies_done.add(cid)
                _post_target("companies", cid, attio_title, title, summary, url, source, dedup_ttl)
    if posted:
        threading.Thread(target=_send_notification,
                         args=(title, ", ".join(posted), source, url), daemon=True).start()
    return posted


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


def _external_attendees(invitees):
    """Return [{'email','name'}] for every external (non-MY_DOMAIN) attendee with a valid email."""
    seen, out = set(), []
    for inv in invitees or []:
        email = (inv.get("email") or "").strip().lower()
        if "@" not in email or email in seen:
            continue
        domain = email.split("@")[1]
        if MY_DOMAIN in domain:
            continue
        seen.add(email)
        name = (inv.get("name") or inv.get("display_name") or inv.get("full_name") or "").strip()
        out.append({"email": email, "name": name or _name_from_email(email)})
    return out


def _name_from_email(email):
    """Derive a display name from an email local part: jorge.mcclees -> 'Jorge Mcclees'."""
    local = email.split("@")[0]
    parts = [p for p in local.replace("_", ".").replace("-", ".").split(".") if p]
    return " ".join(p.capitalize() for p in parts) or email


def _find_or_create_person(email, name=""):
    """Find an Attio person by email, creating one (name + email) if none exists. Returns record_id or None."""
    try:
        cached = _cache_get(f"person:{email}")
        if cached is not None:
            return cached or None
        r = httpx.post(
            "https://api.attio.com/v2/objects/people/records/query",
            headers=ATTIO,
            json={"filter": {"email_addresses": {"email_address": {"$eq": email}}}, "limit": 1},
            timeout=30,
        )
        rows = r.json().get("data", [])
        if rows:
            pid = rows[0]["id"]["record_id"]
            _cache_set(f"person:{email}", pid)
            return pid

        name = name or _name_from_email(email)
        first, _, last = name.partition(" ")
        r = httpx.post(
            "https://api.attio.com/v2/objects/people/records",
            headers=ATTIO,
            json={"data": {"values": {
                "email_addresses": [email],
                "name": [{"first_name": first, "last_name": last, "full_name": name}],
            }}},
            timeout=30,
        )
        if r.status_code >= 300:
            print(f"person create failed for {email}: {r.status_code} {r.text[:200]}", flush=True)
            return None
        pid = r.json()["data"]["id"]["record_id"]
        _cache_set(f"person:{email}", pid)
        print(f"Created Attio person: {name} <{email}> -> {pid}".encode('ascii', 'replace').decode('ascii'), flush=True)
        return pid
    except Exception as e:
        print(f"person lookup/create error for {email}: {e}", flush=True)
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


def _post_note(parent_object, parent_id, title, summary, url, source="Fathom"):
    link_label = f"View {source} notes"
    content = summary + (f"\n\n[{link_label}]({url})" if url else "")
    httpx.post(
        "https://api.attio.com/v2/notes",
        headers=ATTIO,
        json={"data": {
            "parent_object":    parent_object,
            "parent_record_id": parent_id,
            "title":   f"{source}: {title}",
            "format":  "markdown",
            "content": content,
        }},
        timeout=30,
    )
    print(f"{source} note created on {parent_object}/{parent_id}: {title}".encode('ascii', 'replace').decode('ascii'))


def _attio_note_exists(parent_object, parent_id, title):
    r = httpx.get(
        "https://api.attio.com/v2/notes",
        headers=ATTIO,
        params={"parent_object": parent_object, "parent_record_id": parent_id, "limit": 500},
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

    cache_key = f"{slug}:{domain}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify(date=cached)

    company_id = _find_company(domain)
    if not company_id:
        return jsonify(date=None)
    r = httpx.get(
        f"https://api.attio.com/v2/objects/companies/records/{company_id}",
        headers=ATTIO,
        timeout=30,
    )
    entries = r.json().get("data", {}).get("values", {}).get(slug, [])
    result = _fmt_date(entries[0].get("interacted_at")) if entries else None
    _cache_set(cache_key, result)
    return jsonify(date=result)


def _fmt_date(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str[:19])
        return dt.strftime("%-m/%-d/%y")
    except Exception:
        return iso_str


def _find_company(domain):
    cached = _cache_get(f"company:{domain}")
    if cached is not None:
        return cached
    r = httpx.post(
        "https://api.attio.com/v2/objects/companies/records/query",
        headers=ATTIO,
        json={"filter": {"domains": {"domain": {"$eq": domain}}}, "limit": 1},
        timeout=30,
    )
    rows = r.json().get("data", [])
    result = rows[0]["id"]["record_id"] if rows else ""
    _cache_set(f"company:{domain}", result)
    return result or None


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
    # Only include notes that have actual content, not just a title
    chunks = []
    for n in notes:
        content = (n.get("content_plaintext") or n.get("content_markdown") or "").strip()
        if content:
            chunks.append(f"Title: {n.get('title','')}\n{content}")
    text = "\n\n---\n\n".join(chunks).strip()
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
    result = resp.content[0].text.strip()
    # If Claude couldn't find real content, return clean fallback
    no_content_phrases = ["don't have", "no meeting notes", "please provide", "no notes to", "unable to"]
    if any(p in result.lower() for p in no_content_phrases):
        return "No notes"
    return result




@app.route("/granola-sync", methods=["POST"])
def granola_sync_route():
    from granola_sync import sync
    done, skipped = sync()
    return jsonify(done=done, skipped=skipped)


# ── In-app Granola sync scheduler ───────────────────────────────────────────
# Replaces the external Railway cron service, which could not be reliably
# re-armed via the API (deployments stuck at SUCCESS, never SLEEPING).
# Every gunicorn worker starts this thread, but an atomic per-cycle claim file
# (plus the per-note dedup lock) guarantees exactly one sync per interval with
# no duplicate notes — even across all 4 workers.
GRANOLA_SYNC_INTERVAL = int(os.environ.get("GRANOLA_SYNC_INTERVAL", "900"))  # 15 min

def _granola_scheduler():
    time.sleep(30)  # let all workers finish booting before the first run
    while True:
        try:
            cycle_id = int(time.time() // GRANOLA_SYNC_INTERVAL)
            claim = f"/tmp/granola_cycle_{cycle_id}"
            try:
                fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except FileExistsError:
                pass  # another worker already owns this cycle
            else:
                from granola_sync import sync
                done, skipped = sync()
                print(f"[scheduler] cycle {cycle_id}: {done} created, {skipped} skipped", flush=True)
        except Exception as e:
            print(f"[scheduler] error: {e}", flush=True)
        time.sleep(60)  # re-check each minute; cycle_id ensures one run per window

# Only run under Railway (or when explicitly enabled) so local script runs and
# tests don't spawn it.
if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("ENABLE_SCHEDULER"):
    threading.Thread(target=_granola_scheduler, daemon=True).start()
    print("Granola in-app scheduler started", flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
