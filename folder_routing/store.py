"""Durable state in Postgres. Replaces nothing in /tmp: the Attio sync keeps
its own dedup files; this feature never reads or writes them.

Cross-worker and cross-replica safety comes from the database:
  * unique (provider, source_id) on jobs, claimed with FOR UPDATE SKIP LOCKED
  * unique (provider, source_id, destination) on folder ops, changed by compare-and-set
  * named leases for pollers
"""
import contextlib
import json
import re

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MIGRATION_LOCK = 0x46520001  # arbitrary constant for pg_advisory_xact_lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS fr_webhook_deliveries (
  provider text NOT NULL,
  delivery_id text NOT NULL,
  source_id text,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, delivery_id)
);
CREATE TABLE IF NOT EXISTS fr_sources (
  provider text NOT NULL,
  source_id text NOT NULL,
  title text,
  link text,
  fingerprint text,
  slot_fingerprint text,
  snapshot jsonb NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, source_id)
);
CREATE INDEX IF NOT EXISTS fr_sources_fp ON fr_sources (provider, fingerprint);
CREATE INDEX IF NOT EXISTS fr_sources_slot ON fr_sources (slot_fingerprint);
CREATE TABLE IF NOT EXISTS fr_jobs (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  source_id text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  attempts int NOT NULL DEFAULT 0,
  dirty boolean NOT NULL DEFAULT false,
  next_run_at timestamptz NOT NULL DEFAULT now(),
  locked_by text,
  locked_until timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, source_id)
);
CREATE INDEX IF NOT EXISTS fr_jobs_ready ON fr_jobs (status, next_run_at);
CREATE TABLE IF NOT EXISTS fr_decisions (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  source_id text NOT NULL,
  destination text NOT NULL,
  policy_version text NOT NULL,
  decision text NOT NULL,
  reasons jsonb NOT NULL,
  evidence jsonb NOT NULL,
  explanation text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fr_decisions_src ON fr_decisions (provider, source_id);
CREATE TABLE IF NOT EXISTS fr_folder_ops (
  provider text NOT NULL,
  source_id text NOT NULL,
  destination text NOT NULL,
  status text NOT NULL,
  attempts int NOT NULL DEFAULT 0,
  decision_id bigint,
  lease_until timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, source_id, destination)
);
CREATE TABLE IF NOT EXISTS fr_review_queue (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  source_id text NOT NULL,
  destination text NOT NULL,
  title text,
  link text,
  owner_email text,
  reasons jsonb NOT NULL,
  explanation text NOT NULL DEFAULT '',
  policy_version text NOT NULL,
  status text NOT NULL DEFAULT 'open',
  resolved_by text,
  resolved_at timestamptz,
  resolution_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, source_id, destination)
);
CREATE TABLE IF NOT EXISTS fr_checkpoints (
  name text PRIMARY KEY,
  value text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fr_leases (
  name text PRIMARY KEY,
  holder text NOT NULL,
  expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS fr_controls (
  name text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fr_oauth_tokens (
  provider text PRIMARY KEY,
  ciphertext text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fr_audit_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  actor text NOT NULL,
  action text NOT NULL,
  provider text,
  source_id text,
  destination text,
  outcome text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb
);
"""

# Folder-op states a worker may take over to (re)attempt a write.
RETRYABLE_OP_STATES = ("planned", "dry_run", "uncertain", "unverified", "failed_retryable", "review")
TERMINAL_OP_STATES = ("verified", "already_member", "blocked")


class Store:
    def __init__(self, dsn, schema="public"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
            raise ValueError("invalid schema name")
        self.dsn = dsn
        self.schema = schema

    @contextlib.contextmanager
    def conn(self):
        with psycopg.connect(self.dsn, autocommit=False, row_factory=dict_row,
                             options=f"-c search_path={self.schema}", connect_timeout=10) as c:
            yield c

    def migrate(self):
        with self.conn() as c:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            if self.schema != "public":
                c.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            c.execute(SCHEMA)

    # ── webhook deliveries ────────────────────────────────────────────────
    def record_delivery(self, provider, delivery_id, source_id):
        """True the first time a delivery ID is seen; False for retries/duplicates."""
        with self.conn() as c:
            row = c.execute(
                "INSERT INTO fr_webhook_deliveries (provider, delivery_id, source_id) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING delivery_id",
                (provider, delivery_id, source_id)).fetchone()
            return row is not None

    # ── sources ───────────────────────────────────────────────────────────
    def upsert_source(self, meeting):
        with self.conn() as c:
            c.execute(
                "INSERT INTO fr_sources (provider, source_id, title, link, fingerprint, slot_fingerprint, snapshot) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (provider, source_id) DO UPDATE SET "
                "title=EXCLUDED.title, link=EXCLUDED.link, fingerprint=EXCLUDED.fingerprint, "
                "slot_fingerprint=EXCLUDED.slot_fingerprint, snapshot=EXCLUDED.snapshot, last_seen_at=now()",
                (meeting.provider, meeting.source_id, meeting.title[:500], meeting.link,
                 meeting.fingerprint() or None, meeting.slot_fingerprint() or None, Jsonb(meeting.snapshot())))

    def get_source_snapshot(self, provider, source_id):
        with self.conn() as c:
            row = c.execute("SELECT snapshot FROM fr_sources WHERE provider=%s AND source_id=%s",
                            (provider, source_id)).fetchone()
            return row["snapshot"] if row else None

    def find_related_sources(self, meeting):
        """Other recordings that look like the same meeting: same-provider fingerprint, or same slot."""
        fp, slot = meeting.fingerprint(), meeting.slot_fingerprint()
        if not fp and not slot:
            return []
        with self.conn() as c:
            return c.execute(
                "SELECT provider, source_id FROM fr_sources WHERE NOT (provider=%s AND source_id=%s) "
                "AND ((provider=%s AND fingerprint=%s) OR slot_fingerprint=%s) ORDER BY provider, source_id",
                (meeting.provider, meeting.source_id, meeting.provider, fp or None, slot or None)).fetchall()

    # ── jobs ──────────────────────────────────────────────────────────────
    def enqueue(self, provider, source_id):
        """Idempotent. A job already running is marked dirty and re-runs once it finishes."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO fr_jobs (provider, source_id) VALUES (%s,%s) "
                "ON CONFLICT (provider, source_id) DO UPDATE SET "
                "dirty = (fr_jobs.status = 'running'), "
                "status = CASE WHEN fr_jobs.status = 'running' THEN 'running' ELSE 'pending' END, "
                "attempts = CASE WHEN fr_jobs.status IN ('done','dead') THEN 0 ELSE fr_jobs.attempts END, "
                "next_run_at = CASE WHEN fr_jobs.status IN ('done','dead') THEN now() ELSE fr_jobs.next_run_at END, "
                "updated_at = now()",
                (provider, source_id))

    def claim_job(self, worker_id, lease_seconds=300):
        with self.conn() as c:
            return c.execute(
                "UPDATE fr_jobs SET status='running', locked_by=%s, "
                "locked_until=now() + make_interval(secs => %s), attempts=attempts+1, dirty=false, updated_at=now() "
                "WHERE id = (SELECT id FROM fr_jobs WHERE "
                "  (status='pending' AND next_run_at <= now()) OR (status='running' AND locked_until < now()) "
                "  ORDER BY next_run_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING id, provider, source_id, attempts",
                (worker_id, lease_seconds)).fetchone()

    def complete_job(self, job_id, worker_id):
        with self.conn() as c:
            c.execute(
                "UPDATE fr_jobs SET status = CASE WHEN dirty THEN 'pending' ELSE 'done' END, "
                "attempts = CASE WHEN dirty THEN 0 ELSE attempts END, dirty=false, "
                "locked_by=NULL, locked_until=NULL, last_error=NULL, updated_at=now() "
                "WHERE id=%s AND locked_by=%s", (job_id, worker_id))

    def fail_job(self, job_id, worker_id, error_code, retryable, max_attempts, base_seconds=60):
        """Backoff 1, 2, 4 ... minutes (capped at 6h); dead after max_attempts or a permanent error."""
        with self.conn() as c:
            row = c.execute(
                "UPDATE fr_jobs SET "
                "status = CASE WHEN dirty THEN 'pending' WHEN %s AND attempts < %s THEN 'pending' ELSE 'dead' END, "
                "next_run_at = now() + make_interval(secs => LEAST(%s * power(2, GREATEST(attempts-1,0)), 21600)), "
                "dirty=false, locked_by=NULL, locked_until=NULL, last_error=%s, updated_at=now() "
                "WHERE id=%s AND locked_by=%s RETURNING status",
                (retryable, max_attempts, base_seconds, error_code[:200], job_id, worker_id)).fetchone()
            return row["status"] if row else None

    def requeue_ops(self, status):
        with self.conn() as c:
            rows = c.execute("SELECT provider, source_id FROM fr_folder_ops WHERE status=%s", (status,)).fetchall()
        for r in rows:
            self.enqueue(r["provider"], r["source_id"])
        return len(rows)

    # ── decisions / audit ────────────────────────────────────────────────
    def record_decision(self, meeting, destination, decision):
        with self.conn() as c:
            return c.execute(
                "INSERT INTO fr_decisions (provider, source_id, destination, policy_version, decision, reasons, "
                "evidence, explanation) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (meeting.provider, meeting.source_id, destination, decision.policy_version, decision.decision,
                 Jsonb(decision.reasons), Jsonb(decision.evidence), decision.explanation)).fetchone()["id"]

    def latest_decision(self, provider, source_id):
        with self.conn() as c:
            return c.execute(
                "SELECT decision, reasons, policy_version FROM fr_decisions WHERE provider=%s AND source_id=%s "
                "ORDER BY id DESC LIMIT 1", (provider, source_id)).fetchone()

    def audit(self, actor, action, provider=None, source_id=None, destination=None, outcome=None, **details):
        with self.conn() as c:
            c.execute(
                "INSERT INTO fr_audit_log (actor, action, provider, source_id, destination, outcome, details) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (actor, action, provider, source_id, destination, outcome, Jsonb(details)))

    # ── folder ops (idempotency key: provider + source + destination) ────
    def get_op(self, provider, source_id, destination):
        with self.conn() as c:
            return c.execute("SELECT * FROM fr_folder_ops WHERE provider=%s AND source_id=%s AND destination=%s",
                             (provider, source_id, destination)).fetchone()

    def set_op(self, provider, source_id, destination, status, decision_id=None, error=None):
        """Record a non-write state. Never overwrites a terminal state."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO fr_folder_ops (provider, source_id, destination, status, decision_id, last_error) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (provider, source_id, destination) DO UPDATE SET "
                "status=EXCLUDED.status, decision_id=COALESCE(EXCLUDED.decision_id, fr_folder_ops.decision_id), "
                "last_error=EXCLUDED.last_error, lease_until=NULL, updated_at=now() "
                "WHERE fr_folder_ops.status NOT IN ('verified','already_member','blocked') "
                "AND NOT (fr_folder_ops.status='writing' AND fr_folder_ops.lease_until > now())",
                (provider, source_id, destination, status, decision_id, error))

    def claim_op_for_write(self, provider, source_id, destination, decision_id, lease_seconds=120):
        """Compare-and-set to 'writing'. Returns the prior state, or None if another worker holds it
        or it is already terminal. Exactly one caller wins per key."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO fr_folder_ops (provider, source_id, destination, status, decision_id) "
                "VALUES (%s,%s,%s,'planned',%s) ON CONFLICT DO NOTHING",
                (provider, source_id, destination, decision_id))
            row = c.execute(
                "WITH prev AS (SELECT status FROM fr_folder_ops WHERE provider=%s AND source_id=%s AND destination=%s "
                "  FOR UPDATE) "
                "UPDATE fr_folder_ops o SET status='writing', attempts=o.attempts+1, decision_id=%s, "
                "lease_until=now() + make_interval(secs => %s), updated_at=now() FROM prev "
                "WHERE o.provider=%s AND o.source_id=%s AND o.destination=%s AND "
                "(prev.status = ANY(%s) OR (prev.status='writing' AND o.lease_until < now())) "
                "RETURNING prev.status AS prior",
                (provider, source_id, destination, decision_id, lease_seconds, provider, source_id, destination,
                 list(RETRYABLE_OP_STATES))).fetchone()
            return row["prior"] if row else None

    def finish_op(self, provider, source_id, destination, status, error=None):
        with self.conn() as c:
            c.execute(
                "UPDATE fr_folder_ops SET status=%s, last_error=%s, lease_until=NULL, updated_at=now() "
                "WHERE provider=%s AND source_id=%s AND destination=%s AND status='writing'",
                (status, error, provider, source_id, destination))

    # ── review queue ─────────────────────────────────────────────────────
    def upsert_review(self, meeting, destination, decision, extra_reasons=()):
        reasons = list(decision.reasons) + [r for r in extra_reasons if r not in decision.reasons]
        with self.conn() as c:
            c.execute(
                "INSERT INTO fr_review_queue (provider, source_id, destination, title, link, owner_email, reasons, "
                "explanation, policy_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (provider, source_id, destination) DO UPDATE SET title=EXCLUDED.title, "
                "link=EXCLUDED.link, owner_email=EXCLUDED.owner_email, reasons=EXCLUDED.reasons, "
                "explanation=EXCLUDED.explanation, policy_version=EXCLUDED.policy_version, updated_at=now() "
                "WHERE fr_review_queue.status='open'",
                (meeting.provider, meeting.source_id, destination, meeting.title[:500], meeting.link,
                 meeting.owner_email, Jsonb(reasons), decision.explanation, decision.policy_version))

    def review_failure(self, provider, source_id, destination, code, policy_version):
        """Queue a job that exhausted its retries, so it is never silently dropped."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO fr_review_queue (provider, source_id, destination, title, link, reasons, explanation, "
                "policy_version) SELECT %s,%s,%s, s.title, s.link, %s, %s, %s FROM (SELECT 1) one "
                "LEFT JOIN fr_sources s ON s.provider=%s AND s.source_id=%s "
                "ON CONFLICT (provider, source_id, destination) DO UPDATE SET "
                "reasons = fr_review_queue.reasons || EXCLUDED.reasons, updated_at=now() "
                "WHERE fr_review_queue.status='open'",
                (provider, source_id, destination, Jsonb(["processing_failed", code[:100]]),
                 "REVIEW: processing failed after retries", policy_version, provider, source_id))

    def supersede_review(self, provider, source_id, destination, actor="system"):
        with self.conn() as c:
            c.execute(
                "UPDATE fr_review_queue SET status='superseded', resolved_by=%s, resolved_at=now(), updated_at=now() "
                "WHERE provider=%s AND source_id=%s AND destination=%s AND status='open'",
                (actor, provider, source_id, destination))

    def list_review(self, status="open", limit=200):
        with self.conn() as c:
            return c.execute(
                "SELECT id, provider, source_id, destination, title, link, owner_email, reasons, explanation, "
                "policy_version, "
                "status, created_at, updated_at FROM fr_review_queue WHERE status=%s ORDER BY created_at LIMIT %s",
                (status, limit)).fetchall()

    def resolve_review(self, review_id, status, actor, note=""):
        if status not in ("filed_manually", "rejected", "open"):
            raise ValueError("invalid status")
        with self.conn() as c:
            row = c.execute(
                "UPDATE fr_review_queue SET status=%s, resolved_by=%s, resolved_at=now(), resolution_note=%s, "
                "updated_at=now() WHERE id=%s RETURNING provider, source_id, destination",
                (status, actor, note[:500], review_id)).fetchone()
        if row:
            self.audit(actor, "review_resolved", row["provider"], row["source_id"], row["destination"], status)
        return row

    # ── checkpoints / leases / controls ──────────────────────────────────
    def get_checkpoint(self, name):
        with self.conn() as c:
            row = c.execute("SELECT value FROM fr_checkpoints WHERE name=%s", (name,)).fetchone()
            return row["value"] if row else None

    def set_checkpoint(self, name, value):
        with self.conn() as c:
            c.execute("INSERT INTO fr_checkpoints (name, value) VALUES (%s,%s) ON CONFLICT (name) DO UPDATE "
                      "SET value=EXCLUDED.value, updated_at=now()", (name, value))

    def acquire_lease(self, name, holder, ttl_seconds):
        """At most one holder per name until expiry. Not released early when used as a rate limit."""
        with self.conn() as c:
            row = c.execute(
                "INSERT INTO fr_leases (name, holder, expires_at) VALUES (%s,%s, now() + make_interval(secs => %s)) "
                "ON CONFLICT (name) DO UPDATE SET holder=EXCLUDED.holder, expires_at=EXCLUDED.expires_at "
                "WHERE fr_leases.expires_at < now() RETURNING holder",
                (name, holder, ttl_seconds)).fetchone()
            return row is not None

    def get_control(self, name):
        with self.conn() as c:
            row = c.execute("SELECT value FROM fr_controls WHERE name=%s", (name,)).fetchone()
            return row["value"] if row else None

    def set_control(self, name, value):
        with self.conn() as c:
            c.execute("INSERT INTO fr_controls (name, value) VALUES (%s,%s) ON CONFLICT (name) DO UPDATE "
                      "SET value=EXCLUDED.value, updated_at=now()", (name, Jsonb(value)))

    def writes_halted(self):
        value = self.get_control("writes_halted")
        return bool(value and value.get("halted"))

    def halt_writes(self, reason, actor="system"):
        self.set_control("writes_halted", {"halted": True, "reason": reason})
        self.audit(actor, "writes_halted", outcome=reason)

    def resume_writes(self, actor, reason=""):
        self.set_control("writes_halted", {"halted": False, "reason": reason})
        self.audit(actor, "writes_resumed", outcome=reason[:200])

    # ── oauth tokens (ciphertext only) ───────────────────────────────────
    def load_token(self, provider, for_update_conn=None):
        c = for_update_conn
        sql = "SELECT ciphertext FROM fr_oauth_tokens WHERE provider=%s" + (" FOR UPDATE" if c else "")
        if c:
            row = c.execute(sql, (provider,)).fetchone()
        else:
            with self.conn() as c2:
                row = c2.execute(sql, (provider,)).fetchone()
        return row["ciphertext"] if row else None

    def save_token(self, provider, ciphertext, conn=None):
        sql = ("INSERT INTO fr_oauth_tokens (provider, ciphertext) VALUES (%s,%s) ON CONFLICT (provider) "
               "DO UPDATE SET ciphertext=EXCLUDED.ciphertext, updated_at=now()")
        if conn:
            conn.execute(sql, (provider, ciphertext))
        else:
            with self.conn() as c:
                c.execute(sql, (provider, ciphertext))

    # ── reporting ────────────────────────────────────────────────────────
    def status_counts(self):
        with self.conn() as c:
            jobs = c.execute("SELECT status, count(*) AS n FROM fr_jobs GROUP BY status").fetchall()
            ops = c.execute("SELECT status, count(*) AS n FROM fr_folder_ops GROUP BY status").fetchall()
            review = c.execute("SELECT status, count(*) AS n FROM fr_review_queue GROUP BY status").fetchall()
            cps = c.execute("SELECT name, value, updated_at FROM fr_checkpoints ORDER BY name").fetchall()
        return {
            "jobs": {r["status"]: r["n"] for r in jobs},
            "folder_ops": {r["status"]: r["n"] for r in ops},
            "review": {r["status"]: r["n"] for r in review},
            "checkpoints": {r["name"]: {"value": r["value"], "updated_at": r["updated_at"].isoformat()} for r in cps},
        }


def dumps(value):
    return json.dumps(value, default=str)
