"""Candidate interviews, including the PLSA simulation trial, are never filed or queued for filing."""
import pytest

from folder_routing.config import PolicyConfig
from folder_routing.normalize import fathom_meeting_to_source, granola_note_to_source
from folder_routing.pipeline import Pipeline
from folder_routing.policy import AUTO, EXCLUDED, REVIEW, classify
from tests.fakes import PLSA_REST_ID, FakeGranolaReader, FakeWriter, fathom_meeting, granola_note, make_settings

CFG = PolicyConfig()
SOHUM = ("Sohum", "sohum@eagleeng.com")
PLSA = ("Justin Suiter", "jsuiter@plsaengineering.com")
CANDIDATE = ("Jane Doe", "jane.doe@gmail.com")


def fathom(**kw):
    kw.setdefault("invitees", [SOHUM, PLSA])
    kw.setdefault("speakers", [("Sohum", SOHUM[1], 10), ("Justin Suiter", PLSA[1], 10)])
    return classify(fathom_meeting_to_source(fathom_meeting(1, **kw)), CFG)


@pytest.mark.parametrize("kw", [
    {"meeting_type": "Candidate Interview"},
    {"title": "PLSA Simulation - Jane Doe"},
    {"title": "Founder Screen - Jane Doe - Founding Member of Technical Staff"},
    {"title": "Decomposition Exercise - Jane Doe"},
    {"title": "Work trial: Jane"},
    {"invitees": [SOHUM, PLSA, ("Jack", "jack.walsh@metric-search.com")]},
])
def test_interview_signals_exclude_even_with_verified_plsa_speaker(kw):
    d = fathom(**kw)
    assert d.decision == EXCLUDED and d.explanation.startswith("EXCLUDED:")


def test_known_candidate_on_an_otherwise_qualifying_call_is_excluded():
    m = fathom_meeting_to_source(fathom_meeting(1, invitees=[SOHUM, PLSA, CANDIDATE],
                                                speakers=[("Justin Suiter", PLSA[1], 10)]))
    assert classify(m, CFG, known_candidates={CANDIDATE[1]}).reasons == ["known_candidate_present"]


def test_stormwater_modeling_call_is_not_mistaken_for_the_simulation():
    assert fathom(title="PLSA <> Eagle SWMM").decision == AUTO


def test_unidentified_speakers_block_auto_filing():
    d = fathom(speakers=[("Justin Suiter", PLSA[1], 60), ("Someone", None, 40)])
    assert d.decision == REVIEW and "unidentified_speaker" in d.reasons
    assert fathom(speakers=[("Justin Suiter", PLSA[1], 90), ("Someone", None, 10)]).decision == AUTO


# ── pipeline ──────────────────────────────────────────────────────────────

def build(store):
    s = make_settings(granola_writer_emails=("sohum@eagleeng.com", "glang@plsaengineering.com"))
    reader = FakeGranolaReader()
    writer = FakeWriter(reader)
    return s, reader, writer, Pipeline(s, store, granola_reader=reader, granola_writers=lambda owner: writer)


def rows(store, sql, *args):
    with store.conn() as c:
        return c.execute(sql, args).fetchall()


def actions(store):
    return [r["action"] for r in rows(store, "SELECT action FROM fr_audit_log ORDER BY id")]


def test_simulation_note_is_never_written_or_queued_even_with_strong_evidence(store):
    s, reader, writer, pl = build(store)
    reader.add(granola_note("not_X000000000001", title="PLSA simulation - Jane", owner="glang@plsaengineering.com",
                            invitees=["sohum@eagleeng.com", "glang@plsaengineering.com"]))
    assert pl.process("granola", "not_X000000000001") == "excluded"
    assert writer.calls == [] and store.list_review() == []
    assert store.get_op("granola", "not_X000000000001", s.granola_dest.key)["status"] == "excluded"
    assert store.claim_op_for_write("granola", "not_X000000000001", s.granola_dest.key, None) is None


def test_exclusion_is_permanent_even_if_the_signal_disappears(store):
    s, reader, writer, pl = build(store)
    note = granola_note("not_X000000000002", title="Candidate interview", owner="glang@plsaengineering.com",
                        invitees=["sohum@eagleeng.com", "glang@plsaengineering.com"])
    reader.add(note)
    pl.process("granola", "not_X000000000002")
    note["title"] = "Working session"  # renamed later
    assert pl.process("granola", "not_X000000000002") == "excluded"
    assert writer.calls == []


def test_typed_fathom_interview_teaches_candidates_and_pulls_other_notes_out_of_review(store):
    s, reader, writer, pl = build(store)
    # A Granola note that would otherwise be reviewed (PLSA + an unknown outside address).
    reader.add(granola_note("not_X000000000003", owner="sohum@eagleeng.com",
                            invitees=["sohum@eagleeng.com", "glang@plsaengineering.com", CANDIDATE[1]],
                            start="2026-10-25T15:00:00Z"))
    assert pl.process("granola", "not_X000000000003") == "review"
    assert store.list_review()[0]["source_id"] == "not_X000000000003"

    fm = fathom_meeting(77, title="Sohum (Eagle) and Jane Doe", invitees=[SOHUM, CANDIDATE],
                        speakers=[("Sohum", SOHUM[1], 50), ("Jane Doe", CANDIDATE[1], 50)],
                        meeting_type="Candidate Interview")
    store.upsert_source(fathom_meeting_to_source(fm))
    assert pl.process("fathom", "77") == "excluded"
    assert [r for r in store.list_review() if r["provider"] == "fathom"] == []  # no manual-filing item
    assert store.known_candidates({CANDIDATE[1], SOHUM[1]}) == {CANDIDATE[1]}

    job = store.claim_job("w")
    assert (job["provider"], job["source_id"]) == ("granola", "not_X000000000003")
    assert pl.process("granola", "not_X000000000003") == "excluded"
    assert store.list_review() == []
    assert rows(store, "SELECT status FROM fr_review_queue")[0]["status"] == "excluded"


def test_other_recording_of_an_excluded_interview_is_excluded(store):
    s, reader, writer, pl = build(store)
    fm = fathom_meeting(78, title="Sohum (Eagle) and Jane Doe", invitees=[SOHUM, PLSA],
                        speakers=[("Justin Suiter", PLSA[1], 50)], meeting_type="Candidate Interview",
                        start="2026-10-26T17:00:00Z")
    store.upsert_source(fathom_meeting_to_source(fm))
    assert pl.process("fathom", "78") == "excluded"
    reader.add(granola_note("not_X000000000004", owner="sohum@eagleeng.com", start="2026-10-26T13:00:00-04:00",
                            invitees=["sohum@eagleeng.com", "jsuiter@plsaengineering.com"],
                            transcript=(("me", 30), ("them", 30))))
    assert pl.process("granola", "not_X000000000004") == "excluded"
    assert "related_candidate_interview" in store.latest_decision("granola", "not_X000000000004")["reasons"]
    assert writer.calls == []


def test_interview_already_in_folder_is_flagged_never_removed(store):
    s, reader, writer, pl = build(store)
    reader.add(granola_note("not_X000000000005", title="Interview debrief: Jane", owner="sohum@eagleeng.com"),
               folders=[PLSA_REST_ID])
    assert pl.process("granola", "not_X000000000005") == "excluded"
    assert "candidate_interview_in_folder" in actions(store)
    assert reader.members["not_X000000000005"] == {PLSA_REST_ID.lower()} and writer.removals == []


def test_last_check_before_write_refuses_an_interview(store):
    s, reader, writer, pl = build(store)
    note = granola_note("not_X000000000006", title="Founder Screen - Jane", owner="glang@plsaengineering.com",
                        invitees=["sohum@eagleeng.com", "glang@plsaengineering.com"])
    m = granola_note_to_source(dict(note, folder_membership=[]))
    forced = classify(granola_note_to_source(dict(note, title="Working session")), s.policy)
    assert forced.decision == AUTO
    assert pl._write_granola(m, s.granola_dest, forced, None) == "excluded"
    assert writer.calls == []
