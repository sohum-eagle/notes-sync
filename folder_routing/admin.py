"""Admin endpoints. Disabled (404) unless FOLDER_ROUTING_ADMIN_TOKEN is set;
the token is accepted only from the Authorization header, never a query string."""
import hmac

from flask import Blueprint, abort, jsonify, request

from . import runtime

bp = Blueprint("folder_routing_admin", __name__, url_prefix="/admin/folder-routing")


def authorized(req):
    token = runtime.settings().admin_token
    if not token:
        return None  # feature not configured: callers answer 404
    supplied = req.headers.get("Authorization", "")
    return supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:].encode(), token.encode())


def require_admin():
    ok = authorized(request)
    if ok is None:
        abort(404)
    if not ok:
        abort(401)


@bp.before_request
def _guard():
    require_admin()


@bp.get("/status")
def status():
    s = runtime.settings()
    out = {
        "enabled": s.enabled,
        "writes_enabled": s.writes_enabled,
        "granola_writes_enabled": s.granola_writes_enabled,
        "granola_writes_allowed": s.provider_writes_allowed("granola"),
        "fathom_writes_supported": False,
        "granola_folder_mapping_pinned": bool(s.granola_dest.rest_folder_id),
        "granola_integrations_attested": bool(s.granola_integrations_attestation),
        "granola_writer_configured": bool(s.granola_writer_email),
    }
    if s.database_url:
        st = runtime.store()
        out["writes_halted"] = st.get_control("writes_halted") or {"halted": False}
        out.update(st.status_counts())
    return jsonify(out)


@bp.get("/review")
def review():
    rows = runtime.store().list_review(request.args.get("status", "open"))
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
        r["updated_at"] = r["updated_at"].isoformat()
    return jsonify(items=rows)


@bp.post("/review/<int:review_id>")
def resolve(review_id):
    body = request.get_json(silent=True) or {}
    try:
        row = runtime.store().resolve_review(review_id, body.get("status", ""), "admin", body.get("note", ""))
    except ValueError:
        abort(400)
    if not row:
        abort(404)
    return jsonify(ok=True)


@bp.post("/resume-writes")
def resume_writes():
    body = request.get_json(silent=True) or {}
    runtime.store().resume_writes("admin", str(body.get("reason", "")))
    return jsonify(ok=True)
