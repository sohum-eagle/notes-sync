"""Operator commands. All read-only except `migrate`, `requeue`, `resume-writes`
and `granola-oauth-login` (which stores an encrypted token). None writes to a provider.

  python -m folder_routing.cli capability-check
  python -m folder_routing.cli verify-granola-folder-mapping --rest-id fol_... [--expected-uuids-file f]
  python -m folder_routing.cli dry-run --since 2026-10-01T00:00:00Z --out report.md
  python -m folder_routing.cli migrate | requeue --status dry_run | resume-writes --reason ...
  python -m folder_routing.cli granola-oauth-login
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from .config import load_settings
from .http import ApiError
from .normalize import fathom_meeting_to_source, granola_note_to_source, granola_uuid_from_web_url
from .policy import classify, downgrade
from .report import render_markdown


def _granola(s):
    from .providers.granola import GranolaReader
    return GranolaReader(s.granola_api_key) if s.granola_api_key else None


def _mcp_writer(s):
    from .providers.granola import GranolaMcpWriter
    from .providers.mcp_client import McpHttpClient
    from .providers.oauth import DbTokenProvider
    from .store import Store
    st = Store(s.database_url, s.db_schema)
    return GranolaMcpWriter(McpHttpClient(s.granola_mcp_url, DbTokenProvider(st, s.token_encryption_key)))


def capability_check(s, out=print):
    out("== Fathom")
    out("  folder membership read: UNAVAILABLE (no folder endpoints in the documented API)")
    out("  folder write:           UNAVAILABLE (no folder endpoints in the documented API)")
    secret = s.fathom_webhook_secret
    out(f"  webhook secret format:  {'whsec_ (Standard Webhooks, verifiable)' if secret.startswith('whsec_') else ('set, not whsec_' if secret else 'NOT SET')}")
    if s.fathom_api_key:
        from .providers.fathom import FathomReader
        try:
            n = sum(1 for _ in FathomReader(s.fathom_api_key).iter_meetings(include_transcript=False, max_pages=1))
            out(f"  discovery (GET /meetings): OK, {n} meetings on first page")
        except ApiError as e:
            out(f"  discovery (GET /meetings): FAILED {e.code}")
    else:
        out("  discovery: FATHOM_API_KEY not set (webhooks only)")

    out("== Granola REST")
    g = _granola(s)
    if not g:
        out("  GRANOLA_API_KEY not set")
    else:
        try:
            matches = [f for f in g.iter_folders() if f.get("name") == s.granola_dest.expected_title]
            out(f"  folders named {s.granola_dest.expected_title!r}: {[f['id'] for f in matches]}")
            out(f"  pinned GRANOLA_PLSA_FOLDER_REST_ID: {s.granola_dest.rest_folder_id or 'NOT SET'}")
            eps = g.list_webhook_endpoints()
            out(f"  webhook endpoints visible to this key: {len(eps)} "
                f"({sum(1 for e in eps if e.get('folder_ids'))} folder-scoped)")
        except ApiError as e:
            out(f"  FAILED {e.code}")

    out("== Granola MCP (writes)")
    if not (s.database_url and s.token_encryption_key):
        out("  not configured (needs FOLDER_ROUTING_DATABASE_URL, FOLDER_ROUTING_TOKEN_KEY and a login)")
        return
    try:
        folder = _mcp_writer(s).get_folder(s.granola_dest.folder_id)
    except Exception as e:
        out(f"  FAILED {type(e).__name__}: {getattr(e, 'code', '')}")
        return
    if not folder:
        out("  destination folder not visible to this account")
        return
    d = s.granola_dest
    out(f"  folder: title={folder.title!r} visibility={folder.visibility} audience={folder.audience!r} role={folder.role}")
    ok = (folder.title, folder.visibility, folder.audience) == (d.expected_title, d.approved_visibility, d.approved_audience)
    out(f"  matches approved audience config: {'YES' if ok else 'NO -> writes would halt'}")


def verify_mapping(s, rest_id, expected_file=None, out=print):
    g = _granola(s)
    if not g:
        out("GRANOLA_API_KEY not set")
        return 2
    rest = set()
    for n in g.iter_notes(folder_id=rest_id):  # NoteSummary has no web_url, so read each note
        note, _ = g.get_note(n["id"], include_transcript=False)
        if note:
            rest.add(granola_uuid_from_web_url(note.get("web_url")))
    if expected_file:
        expected = set(json.load(open(expected_file)))
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        expected = _mcp_writer(s).list_folder_note_uuids(s.granola_dest.folder_id, "2020-01-01", today)
    out(f"REST {rest_id}: {len(rest)} notes; folder {s.granola_dest.folder_id}: {len(expected)} notes; "
        f"only-REST={len(rest - expected)} only-UUID={len(expected - rest)}")
    if rest and rest == expected:
        out(f"PASS. Pin it: GRANOLA_PLSA_FOLDER_REST_ID={rest_id}")
        return 0
    out("FAIL. Do not pin; membership checks and writes stay disabled.")
    return 1


def dry_run(s, since, provider="both", out_path=None, out=print):
    rows, seen = [], {}
    dest_rest = s.granola_dest.rest_folder_id.lower()
    if provider in ("both", "granola") and s.granola_api_key:
        g = _granola(s)
        for n in g.iter_notes(updated_after=since):
            note, transcript = g.get_note(n["id"])
            if note:
                m = granola_note_to_source(note, transcript)
                rows.append(_row(m, s, seen, member=bool(dest_rest) and dest_rest in m.folder_ids))
    if provider in ("both", "fathom") and s.fathom_api_key:
        from .providers.fathom import FathomReader
        for item in FathomReader(s.fathom_api_key).iter_meetings(created_after=since):
            rows.append(_row(fathom_meeting_to_source(item), s, seen, member=False))
    md = render_markdown(rows, notes=[f"Since {since}.", "Fathom folder membership is not readable via API."])
    if out_path:
        with open(out_path, "w") as f:
            f.write(md)
        out(f"wrote {out_path} ({len(rows)} items)")
    else:
        out(f"{len(rows)} items classified; pass --out to write the report (it contains titles)")
    return rows


def _row(m, s, seen, member):
    d = classify(m, s.policy)
    fp = m.fingerprint()
    if fp and fp in seen.setdefault(m.provider, set()):
        d = downgrade(d, "possible_duplicate_recording")
    if fp:
        seen[m.provider].add(fp)
    return {"provider": m.provider, "source_id": m.source_id, "title": m.title, "link": m.link,
            "date": m.scheduled_start or m.created_at, "decision": d.decision, "reasons": d.reasons,
            "explanation": d.explanation, "already_member": member}


def main(argv=None):
    p = argparse.ArgumentParser(prog="folder_routing")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capability-check")
    sub.add_parser("migrate")
    v = sub.add_parser("verify-granola-folder-mapping")
    v.add_argument("--rest-id", required=True)
    v.add_argument("--expected-uuids-file")
    d = sub.add_parser("dry-run")
    d.add_argument("--since", required=True)
    d.add_argument("--provider", choices=["both", "granola", "fathom"], default="both")
    d.add_argument("--out")
    r = sub.add_parser("requeue")
    r.add_argument("--status", required=True, choices=["dry_run", "review", "failed_retryable", "uncertain", "unverified"])
    w = sub.add_parser("resume-writes")
    w.add_argument("--reason", required=True)
    o = sub.add_parser("granola-oauth-login")
    o.add_argument("--port", type=int, default=8765)
    o.add_argument("--scope")
    args = p.parse_args(argv)
    s = load_settings()

    if args.cmd == "capability-check":
        capability_check(s)
    elif args.cmd == "verify-granola-folder-mapping":
        return verify_mapping(s, args.rest_id, args.expected_uuids_file)
    elif args.cmd == "dry-run":
        dry_run(s, args.since, args.provider, args.out)
    else:
        from .store import Store
        st = Store(s.database_url, s.db_schema)
        if args.cmd == "migrate":
            st.migrate()
        elif args.cmd == "requeue":
            print(f"requeued {st.requeue_ops(args.status)}")
        elif args.cmd == "resume-writes":
            st.resume_writes("cli", args.reason)
        elif args.cmd == "granola-oauth-login":
            from .providers.oauth import interactive_login
            interactive_login(st, s.token_encryption_key, port=args.port, scope=args.scope)
    return 0


if __name__ == "__main__":
    sys.exit(main())
