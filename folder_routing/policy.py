"""Deterministic PLSA attendance policy.

No model calls and no transcript text: decisions use identities, calendar
metadata and speaker attribution counts only, so meeting content cannot steer
them. Signals only move a call toward review, except the strong attendance
signals, which can auto-file only when nothing sensitive is present.
"""
import re

from .models import Decision, email_domain

POLICY_VERSION = "plsa-attendance-v3"

AUTO, REVIEW, SKIP, EXCLUDED = "auto_file", "review", "skip", "excluded"

STRONG = ("plsa_speaker_verified", "plsa_note_owner", "plsa_attendance_corroborated", "plsa_meeting_evident")
MEDIUM = ("plsa_recorder", "plsa_speaker_name_only", "plsa_speaker_below_min_segments")
WEAK = ("plsa_invitee_only", "plsa_attendee_listed", "plsa_title_mention")
BLOCKERS = ("third_party_present", "investor_present", "sensitive_title", "unidentified_speaker")

EXPLAIN = {
    "plsa_speaker_verified": "a transcript speaker was matched by the provider to a verified PLSA email",
    "plsa_note_owner": "the Granola note was captured by a verified PLSA identity",
    "plsa_attendance_corroborated": "another provider's recording of the same calendar slot and invitee list "
                                    "has a provider-matched PLSA speaker",
    "plsa_meeting_evident": "Eagle-captured Granola note where PLSA-affiliated people are at least half of the "
                            "other participants, nobody else outside Eagle was invited, and a conversation was captured",
    "plsa_minority_of_participants": "PLSA-affiliated people are fewer than half of the other participants",
    "no_conversation_captured": "the transcript does not show a conversation with others",
    "plsa_recorder": "the Fathom recorder is a PLSA identity, but no PLSA speaker was matched",
    "plsa_speaker_name_only": "a speaker name matches a PLSA invitee, but the provider did not match the email",
    "plsa_speaker_below_min_segments": "a PLSA speaker was matched but spoke too little to rely on",
    "plsa_invitee_only": "PLSA people were invited, but calendar invitees alone do not prove attendance",
    "plsa_attendee_listed": "PLSA people are on the attendee list, which is calendar-derived and not proof",
    "plsa_title_mention": "the title mentions PLSA, which is not evidence of attendance",
    "third_party_present": "non-PLSA external parties were invited or spoke",
    "investor_present": "investor (LSVP) participants were invited or spoke",
    "sensitive_title": "the title matches a sensitive-topic term",
    "unidentified_speaker": "a large share of the speech came from people the provider could not match to the invite",
    "candidate_interview_type": "Fathom meeting type is a candidate interview; never filed",
    "interview_title": "the title marks a candidate interview, screen, trial or simulation; never filed",
    "known_candidate_present": "a participant previously appeared as a candidate; never filed",
    "recruiter_present": "a recruiter or applicant-tracking address is on the meeting; never filed",
    "related_candidate_interview": "another recording of this meeting was excluded as a candidate interview; never filed",
    "internal_or_investor_only": "only Eagle/LSVP participants; never auto-filed",
    "no_plsa_signal": "no PLSA identity or mention",
    "no_transcript": "no transcript, so attendance cannot be verified",
    "others_spoke_unidentified": "other people spoke, but Granola transcripts do not identify them",
}


class Identities:
    def __init__(self, cfg):
        self.cfg = cfg

    def kind(self, email):
        if not email:
            return ""
        domain = email_domain(email)
        if email in self.cfg.plsa_emails or domain in self.cfg.plsa_domains:
            return "plsa"
        if any(domain == d or domain.endswith("." + d) for d in self.cfg.ignorable_domains):
            return "ignore"
        if domain in self.cfg.internal_domains:
            return "internal"
        if domain in self.cfg.investor_domains:
            return "investor"
        return "external"


def _norm_name(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _title_terms(title, terms):
    lowered = (title or "").lower()
    return sorted({t for t in terms if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", lowered)})


def _conversation_captured(meeting, cfg):
    """Remote call: enough speech with other people's audio. In person: no system audio, long enough capture."""
    total = sum(s.segments for s in meeting.speakers)
    them = sum(s.segments for s in meeting.speakers if s.attribution == "them")
    system_audio = sum(s.segments for s in meeting.speakers if s.source == "speaker")
    if system_audio:
        return total >= cfg.granola_min_segments and them >= cfg.granola_min_them_segments
    return total >= cfg.granola_in_person_min_segments


def participant_emails(meeting):
    emails = {p.email for p in meeting.invitees + meeting.attendees if p.email}
    emails |= {s.matched_email for s in meeting.speakers if s.matched_email}
    if meeting.owner_email:
        emails.add(meeting.owner_email)
    return emails


def exclusion_reasons(meeting, cfg, known_candidates=(), related_excluded=()):
    """Candidate interviews (including the PLSA simulation trial) are never filed or queued for filing."""
    reasons = []
    if (meeting.meeting_type or "").strip().lower() in cfg.candidate_meeting_types:
        reasons.append("candidate_interview_type")
    if _title_terms(meeting.title, cfg.interview_title_terms):
        reasons.append("interview_title")
    emails = participant_emails(meeting)
    if emails & set(known_candidates):
        reasons.append("known_candidate_present")
    if any(email_domain(e) == d or email_domain(e).endswith("." + d) for e in emails for d in cfg.recruiter_domains):
        reasons.append("recruiter_present")
    if related_excluded:
        reasons.append("related_candidate_interview")
    return reasons


def classify(meeting, cfg, corroborated_by=(), known_candidates=(), related_excluded=()):
    """corroborated_by: IDs of other-provider recordings of the same slot whose own decision
    rested on plsa_speaker_verified (looked up by the pipeline, never inferred from text).
    known_candidates / related_excluded feed the candidate-interview exclusion, which wins over everything."""
    excluded = exclusion_reasons(meeting, cfg, known_candidates, related_excluded)
    if excluded:
        return Decision(EXCLUDED, excluded, {"exclusion": excluded}, POLICY_VERSION, explain(EXCLUDED, excluded))
    ids = Identities(cfg)
    reasons, evidence = [], {}
    if corroborated_by:
        reasons.append("plsa_attendance_corroborated")
        evidence["corroborated_by"] = sorted(corroborated_by)

    invitee_kinds = {p.email: ids.kind(p.email) for p in meeting.invitees}
    attendee_kinds = {p.email: ids.kind(p.email) for p in meeting.attendees}
    plsa_invitees = sorted(e for e, k in invitee_kinds.items() if k == "plsa")
    plsa_attendees = sorted(e for e, k in attendee_kinds.items() if k == "plsa" and e not in plsa_invitees)
    plsa_names = {_norm_name(p.name) for p in meeting.invitees + meeting.attendees
                  if p.name and ids.kind(p.email) == "plsa"}

    verified, below_min, name_only = [], [], []
    for s in meeting.speakers:
        if s.matched_email and ids.kind(s.matched_email) == "plsa":
            (verified if s.segments >= cfg.min_speaker_segments else below_min).append(s.matched_email)
        elif not s.matched_email and s.display_name and _norm_name(s.display_name) in plsa_names:
            name_only.append(s.display_name)

    owner_kind = ids.kind(meeting.owner_email)
    if verified:
        reasons.append("plsa_speaker_verified")
        evidence["plsa_speakers"] = sorted(set(verified))
    if meeting.provider == "granola" and owner_kind == "plsa":
        reasons.append("plsa_note_owner")
    if meeting.provider == "fathom" and owner_kind == "plsa" and not verified:
        reasons.append("plsa_recorder")
    if name_only and not verified:
        reasons.append("plsa_speaker_name_only")
        evidence["plsa_speaker_names"] = sorted(set(name_only))
    if below_min and not verified:
        reasons.append("plsa_speaker_below_min_segments")
    if meeting.provider == "granola" and owner_kind == "internal" and not any(r in reasons for r in STRONG):
        # "Clearly with PLSA": an Eagle person's own capture, PLSA-affiliated people are at least half of
        # everyone else on the meeting, and a conversation was recorded. Blockers below still apply.
        others = {e: k for e, k in {**invitee_kinds, **attendee_kinds}.items()
                  if e != meeting.owner_email and k not in ("", "ignore")}
        n_plsa = sum(1 for k in others.values() if k == "plsa")
        if n_plsa:
            evidence["plsa_share_of_others"] = f"{n_plsa}/{len(others)}"
            if n_plsa * 2 < len(others):
                reasons.append("plsa_minority_of_participants")
            elif not _conversation_captured(meeting, cfg):
                reasons.append("no_conversation_captured")
            else:
                reasons.append("plsa_meeting_evident")
    if plsa_invitees and not any(r in reasons for r in STRONG):
        reasons.append("plsa_invitee_only")
    if plsa_attendees and not any(r in reasons for r in STRONG):
        reasons.append("plsa_attendee_listed")
    if plsa_invitees:
        evidence["plsa_invitees"] = plsa_invitees
    if plsa_attendees:
        evidence["plsa_attendees"] = plsa_attendees
    if re.search(r"(?<![a-z])plsa(?![a-z])", (meeting.title or "").lower()):
        reasons.append("plsa_title_mention")

    # Who else is in the room. Speakers' matched emails count as participants too.
    everyone = dict(invitee_kinds)
    everyone.update(attendee_kinds)
    everyone.update({s.matched_email: ids.kind(s.matched_email) for s in meeting.speakers if s.matched_email})
    third = sorted({email_domain(e) for e, k in everyone.items() if k == "external"})
    investors = sorted({email_domain(e) for e, k in everyone.items() if k == "investor"})
    if third:
        reasons.append("third_party_present")
        evidence["third_party_domains"] = third
    if investors:
        reasons.append("investor_present")
        evidence["investor_domains"] = investors
    terms = _title_terms(meeting.title, cfg.sensitive_terms)
    if terms:
        reasons.append("sensitive_title")
        evidence["sensitive_terms"] = terms

    if not meeting.transcript_available:
        reasons.append("no_transcript")
    if meeting.provider == "granola":
        them = sum(s.segments for s in meeting.speakers if s.attribution == "them")
        evidence["granola_segments_them"] = them
        evidence["granola_segments_total"] = sum(s.segments for s in meeting.speakers)
        if them and not any(r in reasons for r in STRONG):
            reasons.append("others_spoke_unidentified")
    if meeting.provider == "fathom":
        evidence["fathom_speakers_matched"] = sum(1 for s in meeting.speakers if s.matched_email)
        total = sum(s.segments for s in meeting.speakers)
        unmatched = sum(s.segments for s in meeting.speakers if not s.matched_email)
        if total and unmatched >= 20 and unmatched / total >= cfg.max_unidentified_share:
            reasons.append("unidentified_speaker")
            evidence["unidentified_share"] = round(unmatched / total, 2)

    kinds = {k for k in everyone.values() if k and k != "ignore"}
    if owner_kind and owner_kind != "ignore":
        kinds.add(owner_kind)

    strong = [r for r in reasons if r in STRONG]
    blockers = [r for r in reasons if r in BLOCKERS]
    plsa_signal = strong or [r for r in reasons if r in MEDIUM + WEAK]

    if strong and not blockers:
        decision = AUTO
    elif plsa_signal:
        decision = REVIEW
    else:
        decision = SKIP
        if kinds and kinds <= {"internal", "investor"}:
            reasons.append("internal_or_investor_only")
        reasons.append("no_plsa_signal")

    return Decision(
        decision=decision,
        reasons=reasons,
        evidence=evidence,
        policy_version=POLICY_VERSION,
        explanation=explain(decision, reasons),
    )


def downgrade(decision, reason, **evidence):
    """Force an auto_file decision to review, keeping the original evidence."""
    reasons = decision.reasons + [reason]
    merged = dict(decision.evidence, **evidence)
    new = REVIEW if decision.decision == AUTO else decision.decision
    return type(decision)(new, reasons, merged, decision.policy_version, explain(new, reasons))


def explain(decision, reasons):
    parts = [EXPLAIN.get(r, r.replace("_", " ")) for r in reasons]
    return f"{decision.upper()}: " + "; ".join(parts) if parts else decision.upper()
