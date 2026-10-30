from dataclasses import replace

import pytest

from folder_routing.normalize import fathom_meeting_to_source
from folder_routing.pipeline import Pipeline, RetryLater
from folder_routing.runtime import Worker
from tests.fakes import PLSA_REST_ID, FakeGranolaReader, FakeWriter, fathom_meeting, granola_note, make_settings

PLSA_OWNER = "glang@plsaengineering.com"
EAGLE = "sohum@eagleeng.com"


def build(store, writes=True, **kw):
    kw.setdefault("granola_writer_emails", (PLSA_OWNER,))
    s = make_settings(writes=writes, **kw)
    reader = FakeGranolaReader()
    writer = FakeWriter(reader)
    pl = Pipeline(s, store, granola_reader=reader, granola_writers=lambda owner: writer)
    return s, reader, writer, pl


def qualifying(note_id="not_Q000000000001", **kw):
    kw.setdefault("owner", PLSA_OWNER)
    kw.setdefault("invitees", [EAGLE, PLSA_OWNER])
    return granola_note(note_id, **kw)


def rows(store, sql, *args):
    with store.conn() as c:
        return c.execute(sql, args).fetchall()


def review_reasons(store):
    return {r["source_id"]: r["reasons"] for r in store.list_review()}


def test_auto_file_writes_once_then_verifies(store):
    s, reader, writer, pl = build(store)
    reader.add(qualifying())
    assert pl.process("granola", "not_Q000000000001") == "verified"
    assert writer.calls == [("not_Q000000000001", writer.folder.folder_id)]
    assert store.get_op("granola", "not_Q000000000001", s.granola_dest.key)["status"] == "verified"
    actions = [r["action"] for r in rows(store, "SELECT action FROM fr_audit_log ORDER BY id")]
    assert actions[-2:] == ["folder_add_sent", "folder_add_verified"]
    # duplicate events / replays: no second write
    assert pl.process("granola", "not_Q000000000001") == "already_member"
    assert len(writer.calls) == 1


def test_writes_disabled_is_dry_run_only(store):
    s, reader, writer, pl = build(store, writes=False)
    reader.add(qualifying())
    assert pl.process("granola", "not_Q000000000001") == "dry_run"
    assert writer.calls == []
    assert {"auto_qualified", "writes_disabled"} <= set(review_reasons(store)["not_Q000000000001"])


def test_existing_membership_including_historical_exception_is_preserved(store):
    s, reader, writer, pl = build(store)
    # Eagle-only note that a person filed by hand: policy would skip it, but it stays put.
    reader.add(granola_note("not_H000000000001", owner=EAGLE, invitees=[EAGLE, "lina@lsvp.com"]),
               folders=[PLSA_REST_ID])
    assert pl.process("granola", "not_H000000000001") == "already_member"
    assert writer.calls == [] and writer.removals == [] and store.list_review() == []
    assert reader.members["not_H000000000001"] == {PLSA_REST_ID.lower()}
    assert rows(store, "SELECT count(*) AS n FROM fr_decisions")[0]["n"] == 0  # not re-litigated


def test_read_only_note_is_blocked_and_never_retried(store):
    s, reader, writer, pl = build(store)
    writer.mode = "reject"
    reader.add(qualifying())
    assert pl.process("granola", "not_Q000000000001") == "blocked"
    assert "write_rejected" in review_reasons(store)["not_Q000000000001"]
    assert pl.process("granola", "not_Q000000000001") == "terminal:blocked"
    assert len(writer.calls) == 1


def test_uncertain_write_that_landed_is_verified_without_resending(store):
    s, reader, writer, pl = build(store)
    writer.mode = "uncertain_applied"
    reader.add(qualifying())
    with pytest.raises(RetryLater):
        pl.process("granola", "not_Q000000000001")
    assert store.get_op("granola", "not_Q000000000001", s.granola_dest.key)["status"] == "uncertain"
    assert pl.process("granola", "not_Q000000000001") == "already_member"
    assert len(writer.calls) == 1


def test_uncertain_write_that_was_lost_is_resent_then_verified(store):
    s, reader, writer, pl = build(store)
    writer.mode = "uncertain_lost"
    reader.add(qualifying())
    with pytest.raises(RetryLater):
        pl.process("granola", "not_Q000000000001")
    writer.mode = "ok"
    assert pl.process("granola", "not_Q000000000001") == "verified"
    assert len(writer.calls) == 2


def test_success_is_never_reported_without_verified_membership(store):
    s, reader, writer, pl = build(store)
    writer.mode = "no_effect"
    reader.add(qualifying())
    with pytest.raises(RetryLater):
        pl.process("granola", "not_Q000000000001")
    assert store.get_op("granola", "not_Q000000000001", s.granola_dest.key)["status"] == "unverified"
    assert "folder_add_verified" not in [r["action"] for r in rows(store, "SELECT action FROM fr_audit_log")]


@pytest.mark.parametrize("change,reason", [
    ({"visibility": "workspace"}, "destination_visibility_changed"),
    ({"audience": "everyone in the workspace"}, "destination_audience_changed"),
    ({"title": "PLSA (old)"}, "destination_title_changed"),
    ({"parent_folder_id": "0dcf909d-0650-4460-84c4-091ef48db5f1"}, "destination_moved"),
])
def test_folder_audience_change_halts_all_writes(store, change, reason):
    s, reader, writer, pl = build(store)
    writer.folder = replace(writer.folder, **change)
    reader.add(qualifying("not_Q000000000001"))
    reader.add(qualifying("not_Q000000000002", uuid="00000000-0000-4000-8000-000000000002",
                           start="2026-10-22T17:00:00Z"))
    assert pl.process("granola", "not_Q000000000001") == "review"
    assert reason in review_reasons(store)["not_Q000000000001"]
    assert store.writes_halted()
    # Folder restored, but the kill switch holds until a person resumes writes.
    writer.folder = FakeWriter(reader).folder
    pl._audience = {}
    assert pl.process("granola", "not_Q000000000002") == "review"
    assert "writes_halted" in review_reasons(store)["not_Q000000000002"]
    assert writer.calls == []
    store.resume_writes("tester", "audience re-approved")
    assert pl.process("granola", "not_Q000000000002") == "verified"


def test_unapproved_downstream_folder_webhook_halts(store):
    s, reader, writer, pl = build(store)
    reader.webhooks = [{"id": "whe_X0000000000001", "folder_ids": [PLSA_REST_ID]}]
    reader.add(qualifying())
    assert pl.process("granola", "not_Q000000000001") == "review"
    assert store.writes_halted() and writer.calls == []
    s2, _, writer2, pl2 = build(store, granola_approved_webhook_ids=("whe_x0000000000001",))
    store.resume_writes("tester")
    w2 = FakeWriter(reader)
    pl2.granola, pl2.writer_for = reader, (lambda owner: w2)
    assert pl2.process("granola", "not_Q000000000001") == "verified"


@pytest.mark.parametrize("override,gate", [
    ({"granola_writer_emails": (EAGLE,)}, "owner_authorization_needed"),
    ({"granola_integrations_attestation": ""}, "folder_integrations_unattested"),
])
def test_write_gates_route_to_review(store, override, gate):
    s, reader, writer, pl = build(store, **override)
    reader.add(qualifying())
    assert pl.process("granola", "not_Q000000000001") == "review"
    assert gate in review_reasons(store)["not_Q000000000001"] and writer.calls == []


def test_unpinned_folder_mapping_blocks_writes(store):
    s, reader, writer, pl = build(store)
    pl.s = replace(s, granola_dest=replace(s.granola_dest, rest_folder_id=""))
    reader.add(qualifying())
    assert pl.process("granola", "not_Q000000000001") == "review"
    assert "folder_mapping_unverified" in review_reasons(store)["not_Q000000000001"]


def test_ambiguous_attendance_goes_to_review_without_writing(store):
    s, reader, writer, pl = build(store, granola_writer_emails=(EAGLE,))
    reader.add(granola_note("not_A000000000001", owner=EAGLE, invitees=[EAGLE, PLSA_OWNER]))
    assert pl.process("granola", "not_A000000000001") == "review"
    assert {"plsa_invitee_only", "others_spoke_unidentified"} <= set(review_reasons(store)["not_A000000000001"])
    assert writer.calls == []


def test_eagle_lsvp_only_call_is_skipped(store):
    s, reader, writer, pl = build(store)
    reader.add(granola_note("not_E000000000001", owner=EAGLE, invitees=[EAGLE, "lina@lsvp.com"]))
    assert pl.process("granola", "not_E000000000001") == "skip"
    assert store.list_review() == [] and writer.calls == []


def test_fathom_auto_qualified_goes_to_manual_queue_with_link(store):
    s, reader, writer, pl = build(store)
    m = fathom_meeting(42, invitees=[("S", EAGLE), ("J", "jsuiter@plsaengineering.com")],
                       speakers=[("J", "jsuiter@plsaengineering.com", 5)])
    store.upsert_source(fathom_meeting_to_source(m))
    assert pl.process("fathom", "42") == "manual_required"
    item = store.list_review()[0]
    assert item["link"] == "https://fathom.video/calls/42"
    assert {"auto_qualified", "manual_filing_required", "fathom_folder_write_unsupported"} <= set(item["reasons"])
    assert writer.calls == []


def test_duplicate_recordings_are_flagged_not_double_filed(store):
    s, reader, writer, pl = build(store)
    reader.add(qualifying("not_D000000000001", cal_id="evt_1"))
    reader.add(qualifying("not_D000000000002", cal_id="evt_1", uuid="00000000-0000-4000-8000-000000000002"))
    assert pl.process("granola", "not_D000000000001") == "verified"
    assert pl.process("granola", "not_D000000000002") == "review"
    assert "possible_duplicate_recording" in review_reasons(store)["not_D000000000002"]
    assert len(writer.calls) == 1


def test_fathom_speaker_evidence_corroborates_granola_note_of_same_slot(store):
    s, reader, writer, pl = build(store, granola_writer_emails=(EAGLE,))
    invitees = [EAGLE, "jsuiter@plsaengineering.com"]
    reader.add(granola_note("not_C000000000001", owner=EAGLE, invitees=invitees, start="2026-10-21T13:00:00-04:00"))
    assert pl.process("granola", "not_C000000000001") == "review"  # no identity evidence yet
    fm = fathom_meeting(7, invitees=[("S", EAGLE), ("J", "jsuiter@plsaengineering.com")],
                        speakers=[("J", "jsuiter@plsaengineering.com", 6)], start="2026-10-21T17:00:00Z")
    store.upsert_source(fathom_meeting_to_source(fm))
    assert pl.process("fathom", "7") == "manual_required"
    job = store.claim_job("w")  # the Fathom evidence re-queued the Granola note
    assert (job["provider"], job["source_id"]) == ("granola", "not_C000000000001")
    assert pl.process("granola", "not_C000000000001") == "verified"
    assert store.latest_decision("granola", "not_C000000000001")["reasons"] == ["plsa_attendance_corroborated"]


def test_manual_removal_is_respected(store):
    s, reader, writer, pl = build(store)
    reader.add(qualifying())
    assert pl.process("granola", "not_Q000000000001") == "verified"
    reader.members["not_Q000000000001"] = set()  # a person removed it from the folder
    assert pl.process("granola", "not_Q000000000001") == "terminal:verified"
    assert len(writer.calls) == 1
    assert "membership_removed_externally" in [r["action"] for r in rows(store, "SELECT action FROM fr_audit_log")]


def test_deleted_note_is_skipped(store):
    s, reader, writer, pl = build(store)
    assert pl.process("granola", "not_GONE00000000001") == "source_unavailable"


def test_worker_retries_api_failures_then_dead_letters_to_review(store):
    s, reader, writer, pl = build(store, max_job_attempts=3, granola_sweep_seconds=0)
    reader.add(qualifying())
    reader.fail_get = 99
    store.enqueue("granola", "not_Q000000000001")
    w = Worker(s, store, pl, worker_id="w1")
    for _ in range(3):
        with store.conn() as c:
            c.execute("UPDATE fr_jobs SET next_run_at = now()")
        assert w.run_once()
    assert rows(store, "SELECT status, last_error FROM fr_jobs")[0] == {"status": "dead", "last_error": "http_503"}
    assert "processing_failed" in review_reasons(store)["not_Q000000000001"]
    assert writer.calls == []


def test_worker_recovers_after_transient_failure(store):
    s, reader, writer, pl = build(store, granola_sweep_seconds=0)
    reader.add(qualifying())
    reader.fail_get = 1
    store.enqueue("granola", "not_Q000000000001")
    w = Worker(s, store, pl, worker_id="w1")
    w.run_once()
    with store.conn() as c:
        c.execute("UPDATE fr_jobs SET next_run_at = now()")
    w.run_once()
    assert rows(store, "SELECT status FROM fr_jobs")[0]["status"] == "done"
    assert len(writer.calls) == 1


def test_sweep_runs_once_per_interval_across_workers_and_checkpoints(store):
    s, reader, writer, pl = build(store, writes=False, granola_sweep_seconds=3600)
    reader.add(qualifying("not_S000000000001"))
    reader.add(qualifying("not_S000000000002", uuid="00000000-0000-4000-8000-000000000002",
                           start="2026-10-22T17:00:00Z"))
    a, b = Worker(s, store, pl, worker_id="a"), Worker(s, store, pl, worker_id="b")
    a._sweep("granola")
    b._sweep("granola")  # lease held: no second sweep
    assert rows(store, "SELECT count(*) AS n FROM fr_jobs")[0]["n"] == 2
    assert store.get_checkpoint("granola_sweep_since")


def test_writer_auth_failure_halts_writes_without_blocking_the_note(store):
    from folder_routing.providers.oauth import TokenUnavailable
    s, reader, writer, pl = build(store)
    reader.add(qualifying())

    def expired(*a):
        raise TokenUnavailable("mcp_http_401")
    writer.add = expired
    assert pl.process("granola", "not_Q000000000001") == "owner_login_unavailable"
    assert not store.writes_halted()  # only this owner's notes wait
    assert store.get_op("granola", "not_Q000000000001", s.granola_dest.key)["status"] == "failed_retryable"


def test_writes_enabled_without_writer_goes_to_review(store):
    s, reader, writer, pl = build(store)
    pl.writer_for = lambda owner: None
    reader.add(qualifying())
    assert pl.process("granola", "not_Q000000000001") == "review"
    assert "writer_unconfigured" in review_reasons(store)["not_Q000000000001"]


DANNY = "danny@eagleeng.com"


def evident_note(note_id, owner, start="2026-10-27T17:00:00Z", uuid="00000000-0000-4000-8000-0000000000aa"):
    return granola_note(note_id, owner=owner, invitees=[owner, "slouie@plsaengineering.com"],
                        transcript=(("me", 30), ("them", 30)), start=start, uuid=uuid)


def test_teammate_note_is_filed_only_with_that_teammates_own_login(store):
    s, reader, writer, pl = build(store, granola_writer_emails=(EAGLE, DANNY))
    used = []
    pl.writer_for = lambda owner: used.append(owner) or writer
    reader.add(evident_note("not_T000000000001", DANNY))
    assert pl.process("granola", "not_T000000000001") == "verified"
    assert used == [DANNY]


def test_teammate_without_login_goes_to_review_tagged_with_owner(store):
    s, reader, writer, pl = build(store, granola_writer_emails=(EAGLE,))
    reader.add(evident_note("not_T000000000002", DANNY))
    assert pl.process("granola", "not_T000000000002") == "review"
    item = store.list_review()[0]
    assert item["owner_email"] == DANNY and "owner_authorization_needed" in item["reasons"]
    assert writer.calls == []


def test_owner_without_folder_access_does_not_halt_other_owners(store):
    s, reader, writer, pl = build(store, granola_writer_emails=(EAGLE, DANNY))
    viewer = FakeWriter(reader, folder=replace(writer.folder, role="viewer"))
    pl.writer_for = lambda owner: viewer if owner == DANNY else writer
    reader.add(evident_note("not_T000000000003", DANNY))
    reader.add(evident_note("not_T000000000004", EAGLE, start="2026-10-28T17:00:00Z",
                            uuid="00000000-0000-4000-8000-0000000000bb"))
    assert pl.process("granola", "not_T000000000003") == "review"
    assert "owner_lacks_folder_role" in review_reasons(store)["not_T000000000003"]
    assert not store.writes_halted()
    assert pl.process("granola", "not_T000000000004") == "verified"
    assert viewer.calls == []
