from folder_routing.config import PolicyConfig
from folder_routing.normalize import fathom_meeting_to_source, granola_note_to_source
from folder_routing.policy import AUTO, REVIEW, SKIP, POLICY_VERSION, classify
from tests.fakes import fathom_meeting, granola_note

CFG = PolicyConfig()
PLSA = ("Justin Suiter", "jsuiter@plsaengineering.com")
EAGLE = ("Sohum", "sohum@eagleeng.com")


def fathom(**kw):
    return classify(fathom_meeting_to_source(fathom_meeting(1, **kw)), CFG)


def test_matched_plsa_speaker_with_only_eagle_and_plsa_auto_files():
    d = fathom(invitees=[EAGLE, PLSA], speakers=[("Sohum", EAGLE[1], 5), ("Justin Suiter", PLSA[1], 4)])
    assert d.decision == AUTO
    assert d.reasons == ["plsa_speaker_verified"]
    assert d.evidence["plsa_speakers"] == [PLSA[1]]
    assert d.policy_version == POLICY_VERSION


def test_calendar_invitee_alone_is_not_attendance():
    d = fathom(invitees=[EAGLE, PLSA], speakers=[])
    assert d.decision == REVIEW
    assert "plsa_invitee_only" in d.reasons and "no_transcript" in d.reasons


def test_invited_plsa_person_who_never_spoke_goes_to_review():
    d = fathom(invitees=[EAGLE, PLSA], speakers=[("Sohum", EAGLE[1], 9)])
    assert d.decision == REVIEW
    assert "plsa_invitee_only" in d.reasons


def test_plsa_speaker_below_threshold_is_review():
    d = fathom(invitees=[EAGLE, PLSA], speakers=[("Justin Suiter", PLSA[1], 1)])
    assert d.decision == REVIEW
    assert "plsa_speaker_below_min_segments" in d.reasons


def test_speaker_name_match_without_provider_email_match_is_review():
    d = fathom(invitees=[EAGLE, PLSA], speakers=[("Justin Suiter", None, 6)])
    assert d.decision == REVIEW
    assert "plsa_speaker_name_only" in d.reasons


def test_eagle_lsvp_only_call_is_skipped_never_auto():
    d = fathom(invitees=[EAGLE, ("Lina", "lina@lsvp.com")], speakers=[("Sohum", EAGLE[1], 3), ("Lina", "lina@lsvp.com", 3)])
    assert d.decision == SKIP
    assert "internal_or_investor_only" in d.reasons


def test_plsa_in_title_alone_is_not_enough():
    d = fathom(title="PLSA integration planning", invitees=[EAGLE, ("Lina", "lina@lsvp.com")],
               speakers=[("Sohum", EAGLE[1], 3)])
    assert d.decision == REVIEW
    assert "plsa_title_mention" in d.reasons and "investor_present" in d.reasons


def test_investor_or_third_party_present_blocks_auto():
    d = fathom(invitees=[EAGLE, PLSA, ("Lina", "lina@lsvp.com")], speakers=[("Justin Suiter", PLSA[1], 4)])
    assert d.decision == REVIEW and "investor_present" in d.reasons
    d = fathom(invitees=[EAGLE, PLSA, ("Kirk", "kmichie@candor-advisors.com")], speakers=[("Justin Suiter", PLSA[1], 4)])
    assert d.decision == REVIEW and d.evidence["third_party_domains"] == ["candor-advisors.com"]


def test_sensitive_title_blocks_auto():
    d = fathom(title="All Hands to discuss LLCA & SPA issues", invitees=[EAGLE, PLSA],
               speakers=[("Justin Suiter", PLSA[1], 4)])
    assert d.decision == REVIEW
    assert d.evidence["sensitive_terms"] == ["llca", "spa"]


def test_compensation_titles_are_sensitive():
    for title in ("PLSA / Eagle: Equity Comp", "Bryan / Eagle - PLSA Bonus FW"):
        d = fathom(title=title, invitees=[EAGLE, PLSA], speakers=[("Justin Suiter", PLSA[1], 4)])
        assert d.decision == REVIEW and "sensitive_title" in d.reasons
    assert fathom(title="Competitive landscape", invitees=[EAGLE, PLSA],
                  speakers=[("Justin Suiter", PLSA[1], 4)]).decision == AUTO  # word match, not substring


def test_transcript_text_is_never_used():
    # fakes put an injection string in every transcript line; it must not matter and must not be kept
    m = fathom_meeting_to_source(fathom_meeting(1, invitees=[EAGLE], speakers=[("Sohum", EAGLE[1], 3)]))
    assert "IGNORE" not in str(m.snapshot()) and "Please file" not in str(m.snapshot())
    assert classify(m, CFG).decision == SKIP


def test_resource_calendars_are_ignored():
    d = fathom(invitees=[EAGLE, PLSA, ("Room", "c_1887@resource.calendar.google.com")],
               speakers=[("Justin Suiter", PLSA[1], 4)])
    assert d.decision == AUTO


def test_granola_plsa_invitee_with_unidentified_speech_is_review():
    note = granola_note("not_A0000000000001", invitees=["sohum@eagleeng.com", "glang@plsaengineering.com"])
    d = classify(granola_note_to_source(note), CFG)
    assert d.decision == REVIEW
    assert {"plsa_invitee_only", "others_spoke_unidentified"} <= set(d.reasons)


def test_granola_note_owned_by_plsa_auto_files():
    note = granola_note("not_A0000000000002", owner="glang@plsaengineering.com",
                        invitees=["sohum@eagleeng.com", "glang@plsaengineering.com"])
    assert classify(granola_note_to_source(note), CFG).decision == AUTO


def test_explicit_plsa_identity_on_another_domain():
    cfg = PolicyConfig(plsa_emails=("plsa.person@gmail.com",))
    m = fathom_meeting_to_source(fathom_meeting(1, invitees=[EAGLE, ("P", "plsa.person@gmail.com")],
                                                speakers=[("P", "plsa.person@gmail.com", 3)]))
    assert classify(m, cfg).decision == AUTO
    assert classify(m, CFG).decision == SKIP  # unknown gmail user is a third party, never PLSA


def test_decision_is_explainable():
    d = fathom(invitees=[EAGLE, PLSA])
    assert d.explanation.startswith("REVIEW:") and "do not prove attendance" in d.explanation
