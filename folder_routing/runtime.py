"""Process-wide wiring: settings, store, pipeline and the background worker.

Each gunicorn worker (and each replica) may run a worker thread; they
coordinate only through Postgres (job claims, op compare-and-set, leases).
"""
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import log
from .config import load_settings
from .http import ApiError
from .normalize import fathom_meeting_to_source
from .pipeline import Pipeline, RetryLater

_lock = threading.Lock()
_state = {"settings": None, "store": None, "pipeline": None, "migrated": False}


def configure(settings=None, store=None, pipeline=None):
    """Tests and the CLI inject dependencies here."""
    with _lock:
        _state.update(settings=settings, store=store, pipeline=pipeline, migrated=store is not None)


def settings():
    if _state["settings"] is None:
        _state["settings"] = load_settings()
    return _state["settings"]


def store():
    with _lock:
        if _state["store"] is None:
            from .store import Store
            s = settings()
            if not s.database_url:
                raise RuntimeError("FOLDER_ROUTING_DATABASE_URL is not set")
            _state["store"] = Store(s.database_url, s.db_schema)
        if not _state["migrated"]:
            _state["store"].migrate()
            _state["migrated"] = True
        return _state["store"]


def build_pipeline(s=None, st=None, actor="worker"):
    s = s or settings()
    st = st or store()
    fathom = granola = writers = None
    if s.fathom_api_key:
        from .providers.fathom import FathomReader
        fathom = FathomReader(s.fathom_api_key)
    if s.granola_api_key:
        from .providers.granola import GranolaReader
        granola = GranolaReader(s.granola_api_key)
    if s.provider_writes_allowed("granola") and s.token_encryption_key:
        from .providers.granola import GranolaMcpWriter
        from .providers.mcp_client import McpHttpClient
        from .providers.oauth import DbTokenProvider
        cache = {}

        def writers(owner):
            # One MCP client per owner, each using only that owner's own login.
            if owner not in s.granola_writer_emails:
                return None
            if owner not in cache:
                cache[owner] = GranolaMcpWriter(McpHttpClient(
                    s.granola_mcp_url, DbTokenProvider(st, s.token_encryption_key, account=owner)))
            return cache[owner]
    return Pipeline(s, st, fathom, granola, writers, actor=actor)


def pipeline():
    if _state["pipeline"] is None:
        _state["pipeline"] = build_pipeline()
    return _state["pipeline"]


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Worker:
    def __init__(self, s, st, pl, worker_id=None, sleep=time.sleep):
        self.s, self.store, self.pipeline = s, st, pl
        self.id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.sleep = sleep

    def run_once(self):
        """Sweeps (if their lease is free) then at most one job. Returns True if a job ran."""
        self._sweep("granola")
        self._sweep("fathom")
        job = self.store.claim_job(self.id)
        if not job:
            return False
        try:
            outcome = self.pipeline.process(job["provider"], job["source_id"])
            self.store.complete_job(job["id"], self.id)
            log(f"job {job['id']} {job['provider']}:{job['source_id']} -> {outcome}")
        except RetryLater as e:
            self._failed(job, e.code, True)
        except ApiError as e:
            self._failed(job, e.code, e.retryable)
        except Exception as e:  # bounded by max attempts, then dead + review
            self._failed(job, f"error:{type(e).__name__}", True)
        return True

    def _failed(self, job, code, retryable):
        status = self.store.fail_job(job["id"], self.id, code, retryable, self.s.max_job_attempts)
        log(f"job {job['id']} {job['provider']}:{job['source_id']} failed code={code} -> {status}")
        if status == "dead":
            from .policy import POLICY_VERSION
            dest = self.s.destination(job["provider"]).key
            self.store.audit("worker", "job_dead", job["provider"], job["source_id"], dest, outcome=code)
            self.store.review_failure(job["provider"], job["source_id"], dest, code, POLICY_VERSION)

    def _sweep(self, provider):
        interval = self.s.granola_sweep_seconds if provider == "granola" else self.s.fathom_sweep_seconds
        reader = self.pipeline.granola if provider == "granola" else self.pipeline.fathom
        if interval <= 0 or reader is None:
            return
        if not self.store.acquire_lease(f"{provider}_sweep", self.id, interval):
            return  # another worker swept within the interval
        name = f"{provider}_sweep_since"
        started = datetime.now(timezone.utc)
        cp = self.store.get_checkpoint(name)
        base = datetime.fromisoformat(cp.replace("Z", "+00:00")) if cp else \
            started - timedelta(minutes=self.s.initial_lookback_minutes)
        overlap = self.s.granola_sweep_overlap_minutes if provider == "granola" else self.s.fathom_sweep_overlap_minutes
        since = _iso(base - timedelta(minutes=overlap))
        n = 0
        try:
            if provider == "granola":
                for note in reader.iter_notes(updated_after=since):
                    if str(note.get("id", "")).lower().startswith("not_"):
                        self.store.enqueue("granola", note["id"])
                        n += 1
            else:
                for m in reader.iter_meetings(created_after=since, include_transcript=True):
                    self.store.upsert_source(fathom_meeting_to_source(m))
                    self.store.enqueue("fathom", str(int(m["recording_id"])))
                    n += 1
        except ApiError as e:
            log(f"{provider} sweep failed code={e.code}; checkpoint unchanged")
            return
        self.store.set_checkpoint(name, _iso(started))
        log(f"{provider} sweep enqueued={n}")

    def run_forever(self, stop=None):
        time.sleep(5)
        while not (stop and stop.is_set()):
            try:
                ran = self.run_once()
            except Exception as e:
                log(f"worker loop error={type(e).__name__}")
                ran = False
            if not ran:
                self.sleep(self.s.worker_poll_seconds)


def start_background_worker():
    s = settings()
    if not s.enabled:
        return None
    try:
        w = Worker(s, store(), pipeline())
    except Exception as e:
        log(f"worker not started: {type(e).__name__}")
        return None
    t = threading.Thread(target=w.run_forever, daemon=True, name="folder-routing")
    t.start()
    log(f"worker started writes_allowed={s.provider_writes_allowed('granola')}")
    return t
