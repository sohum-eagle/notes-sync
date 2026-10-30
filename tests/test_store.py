import multiprocessing
import threading

import psycopg

from folder_routing.normalize import fathom_meeting_to_source
from folder_routing.store import Store
from tests.fakes import fathom_meeting


def _rows(st, sql, *args):
    with st.conn() as c:
        return c.execute(sql, args).fetchall()


def test_duplicate_webhook_delivery_is_recorded_once(store):
    assert store.record_delivery("fathom", "msg_1", "123") is True
    assert store.record_delivery("fathom", "msg_1", "123") is False
    assert store.record_delivery("granola", "msg_1", "not_A") is True  # namespaced by provider


def test_enqueue_is_idempotent_per_source(store):
    for _ in range(5):
        store.enqueue("fathom", "123")
    assert _rows(store, "SELECT count(*) AS n FROM fr_jobs")[0]["n"] == 1


def test_identical_titles_are_separate_sources(store):
    a = fathom_meeting_to_source(fathom_meeting(1, title="Weekly sync"))
    b = fathom_meeting_to_source(fathom_meeting(2, title="Weekly sync"))
    store.upsert_source(a)
    store.upsert_source(b)
    store.enqueue("fathom", a.source_id)
    store.enqueue("fathom", b.source_id)
    assert _rows(store, "SELECT count(*) AS n FROM fr_jobs")[0]["n"] == 2
    assert _rows(store, "SELECT count(*) AS n FROM fr_sources")[0]["n"] == 2


def test_concurrent_workers_claim_each_job_once(store):
    for i in range(40):
        store.enqueue("fathom", str(i))
    claimed, lock = [], threading.Lock()

    def worker(wid):
        while True:
            job = store.claim_job(f"w{wid}")
            if not job:
                return
            with lock:
                claimed.append(job["source_id"])
            store.complete_job(job["id"], f"w{wid}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(claimed, key=int) == [str(i) for i in range(40)]


def test_only_one_worker_wins_a_folder_op(store):
    winners, lock, barrier = [], threading.Lock(), threading.Barrier(10)

    def worker():
        barrier.wait()
        prior = store.claim_op_for_write("granola", "not_A", "dest", None)
        if prior is not None:
            with lock:
                winners.append(prior)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert winners == ["planned"]
    store.finish_op("granola", "not_A", "dest", "verified")
    assert store.claim_op_for_write("granola", "not_A", "dest", None) is None  # terminal


def test_set_op_never_overwrites_terminal_state(store):
    store.claim_op_for_write("granola", "not_A", "dest", None)
    store.finish_op("granola", "not_A", "dest", "verified")
    store.set_op("granola", "not_A", "dest", "review")
    assert store.get_op("granola", "not_A", "dest")["status"] == "verified"


def test_running_job_reenqueued_runs_again(store):
    store.enqueue("granola", "not_A")
    job = store.claim_job("w1")
    store.enqueue("granola", "not_A")  # note updated while processing
    store.complete_job(job["id"], "w1")
    again = store.claim_job("w2")
    assert again and again["source_id"] == "not_A"


def test_retry_backoff_then_dead(store):
    store.enqueue("granola", "not_A")
    for attempt in range(1, 4):
        with store.conn() as c:
            c.execute("UPDATE fr_jobs SET next_run_at = now()")
        job = store.claim_job("w")
        assert job["attempts"] == attempt
        status = store.fail_job(job["id"], "w", "http_503", True, max_attempts=3)
        assert status == ("dead" if attempt == 3 else "pending")
    assert store.claim_job("w") is None


def test_permanent_error_goes_dead_immediately(store):
    store.enqueue("granola", "not_A")
    job = store.claim_job("w")
    assert store.fail_job(job["id"], "w", "http_403", False, max_attempts=6) == "dead"


def test_stale_running_job_is_reclaimed(store):
    store.enqueue("granola", "not_A")
    store.claim_job("crashed", lease_seconds=0)
    job = store.claim_job("w2")
    assert job is not None and job["attempts"] == 2
    store.complete_job(job["id"], "crashed")  # fenced: wrong holder changes nothing
    assert _rows(store, "SELECT status FROM fr_jobs")[0]["status"] == "running"


def test_lease_has_one_holder_until_expiry(store):
    assert store.acquire_lease("sweep", "a", 60)
    assert not store.acquire_lease("sweep", "b", 60)
    with store.conn() as c:
        c.execute("UPDATE fr_leases SET expires_at = now() - interval '1 second'")
    assert store.acquire_lease("sweep", "b", 60)


def test_resolved_review_is_not_reopened(store):
    from folder_routing.models import Decision
    m = fathom_meeting_to_source(fathom_meeting(1))
    d = Decision("review", ["plsa_invitee_only"], {}, "v1", "REVIEW")
    store.upsert_review(m, "dest", d)
    rid = store.list_review()[0]["id"]
    store.resolve_review(rid, "rejected", "tester", "not PLSA")
    store.upsert_review(m, "dest", d)
    assert store.list_review() == []
    assert _rows(store, "SELECT status FROM fr_review_queue")[0]["status"] == "rejected"


def _proc_claim(dsn, schema, wid, out):
    st = Store(dsn, schema)
    got = []
    while True:
        job = st.claim_job(wid)
        if not job:
            break
        prior = st.claim_op_for_write(job["provider"], job["source_id"], "dest", None)
        if prior is not None:
            with st.conn() as c:  # stands in for the provider write
                c.execute("INSERT INTO test_writes (source_id, worker) VALUES (%s, %s)", (job["source_id"], wid))
            st.finish_op(job["provider"], job["source_id"], "dest", "verified")
        st.complete_job(job["id"], wid)
        got.append(job["source_id"])
    out.put(got)


def test_separate_processes_write_each_key_once(store):
    """Simulates gunicorn workers / replicas: separate processes, separate connections."""
    with store.conn() as c:
        c.execute("CREATE TABLE test_writes (source_id text, worker text)")
    for i in range(30):
        store.enqueue("granola", f"not_{i:014d}")
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_proc_claim, args=(store.dsn, store.schema, f"p{i}", out)) for i in range(4)]
    [p.start() for p in procs]
    results = [out.get(timeout=60) for _ in procs]
    [p.join(timeout=60) for p in procs]
    assert sum(len(r) for r in results) == 30
    writes = _rows(store, "SELECT source_id, count(*) AS n FROM test_writes GROUP BY source_id")
    assert len(writes) == 30 and all(w["n"] == 1 for w in writes)
