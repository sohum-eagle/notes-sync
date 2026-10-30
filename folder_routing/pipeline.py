"""One job = one source object (Fathom recording or Granola note) against its PLSA folder.

Order of checks, each of which can only make the outcome more conservative:
  1. existing membership  -> recorded, never changed (historical exceptions stay put)
  2. policy classification -> auto_file / review / skip, with evidence + policy version
  3. duplicate recordings  -> auto_file downgraded to review
  4. write gates           -> flags, kill switch, ownership, attestation, audience
  5. compare-and-set claim -> one writer per (provider, source, destination)
  6. additive write        -> then membership verified before anything reports success
"""
import time

from . import log
from .http import ApiError
from .models import SourceMeeting
from .normalize import fathom_speakers, granola_note_to_source
from .policy import AUTO, EXCLUDED, REVIEW, SKIP, Identities, classify, downgrade, exclusion_reasons, \
    participant_emails
from .providers import CapabilityUnavailable, WriteRejected, WriteUncertain
from .providers.mcp_client import McpError
from .providers.oauth import TokenUnavailable


class RetryLater(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Pipeline:
    def __init__(self, settings, store, fathom_reader=None, granola_reader=None, granola_writers=None,
                 actor="worker", clock=time.time, audience_ttl=300):
        """granola_writers: callable(owner_email) -> writer acting with that owner's own login, or None."""
        self.s = settings
        self.store = store
        self.fathom = fathom_reader
        self.granola = granola_reader
        self.writer_for = granola_writers or (lambda email: None)
        self.actor = actor
        self.clock = clock
        self.audience_ttl = audience_ttl
        self._audience = {}  # owner -> (checked_at, (problem, scope))

    # ── loading ──────────────────────────────────────────────────────────
    def load(self, provider, source_id):
        if provider == "granola":
            if self.granola is None:
                raise RetryLater("granola_reader_unconfigured")
            try:
                note, transcript = self.granola.get_note(source_id)
            except ApiError as e:
                raise RetryLater(e.code) if e.retryable else e
            if note is None or note.get("deleted_at"):
                return None
            return granola_note_to_source(note, transcript)
        snap = self.store.get_source_snapshot(provider, source_id)
        if snap is None:
            return None
        meeting = SourceMeeting.from_snapshot(snap)
        if not meeting.transcript_available and self.fathom is not None:
            try:
                items = self.fathom.get_transcript(source_id)
            except ApiError as e:
                if e.retryable:
                    raise RetryLater(e.code)
                items = []
            if items:
                meeting.speakers = fathom_speakers(items)
                meeting.transcript_available = True
        return meeting

    # ── main entry ───────────────────────────────────────────────────────
    def process(self, provider, source_id):
        meeting = self.load(provider, source_id)
        if meeting is None:
            self.store.audit(self.actor, "source_unavailable", provider, source_id, outcome="skip")
            return "source_unavailable"
        dest = self.s.destination(provider)
        key = (provider, source_id, dest.key)
        self.store.upsert_source(meeting)
        op = self.store.get_op(*key)
        related = self.store.find_related_sources(meeting)

        # Candidate interviews (including the PLSA simulation) come first and override everything:
        # never filed, never queued for manual filing, never reconsidered.
        known, related_excluded = self._exclusion_inputs(meeting, related)
        if exclusion_reasons(meeting, self.s.policy, known, related_excluded) or (op and op["status"] == "excluded"):
            return self._exclude(meeting, dest, op, related, known, related_excluded)

        if self._is_member_now(meeting, dest):
            if not op or op["status"] not in ("already_member", "verified"):
                self.store.set_op(*key, "already_member")
                self.store.audit(self.actor, "existing_membership_preserved", *key, outcome="noop")
            self.store.supersede_review(*key)
            return "already_member"
        if op and op["status"] in ("verified", "already_member", "blocked"):
            if op["status"] != "blocked" and self._membership_readable(meeting, dest):
                # Someone removed it by hand. Respect that; never re-add.
                self.store.audit(self.actor, "membership_removed_externally", *key, outcome="noop")
            return f"terminal:{op['status']}"

        same = [f"{r['provider']}:{r['source_id']}" for r in related if r["provider"] == provider]
        others = [r for r in related if r["provider"] != provider]
        corroborated = []
        for r in others:
            prior = self.store.latest_decision(r["provider"], r["source_id"])
            if prior and "plsa_speaker_verified" in prior["reasons"]:
                corroborated.append(f"{r['provider']}:{r['source_id']}")
        decision = classify(meeting, self.s.policy, corroborated_by=corroborated)
        if others:
            decision.evidence["also_recorded_in"] = [f"{r['provider']}:{r['source_id']}" for r in others]
        if same:
            decision = downgrade(decision, "possible_duplicate_recording", duplicate_of=same)
        decision_id = self.store.record_decision(meeting, dest.key, decision)
        log(f"{provider}:{source_id} decision={decision.decision} policy={decision.policy_version} "
            f"reasons={','.join(decision.reasons)}")
        if "plsa_speaker_verified" in decision.reasons:
            for r in others:  # let the other provider's copy re-evaluate with this evidence
                self.store.enqueue(r["provider"], r["source_id"])

        if decision.decision == SKIP:
            return "skip"
        if decision.decision == REVIEW:
            return self._review(meeting, dest, decision, decision_id)
        if provider == "fathom":
            return self._review(meeting, dest, decision, decision_id,
                                ["auto_qualified", "manual_filing_required", "fathom_folder_write_unsupported"],
                                op_status="manual_required")
        if not self.s.provider_writes_allowed(provider):
            return self._review(meeting, dest, decision, decision_id,
                                ["auto_qualified", "writes_disabled"], op_status="dry_run")
        return self._write_granola(meeting, dest, decision, decision_id)

    # ── candidate interviews ─────────────────────────────────────────────
    def _exclusion_inputs(self, meeting, related):
        known = self.store.known_candidates(participant_emails(meeting))
        excluded = []
        for r in related:
            prior = self.store.latest_decision(r["provider"], r["source_id"])
            if prior and prior["decision"] == EXCLUDED:
                excluded.append(f"{r['provider']}:{r['source_id']}")
        return known, excluded

    def _exclude(self, meeting, dest, op, related, known, related_excluded):
        key = (meeting.provider, meeting.source_id, dest.key)
        decision = classify(meeting, self.s.policy, known_candidates=known, related_excluded=related_excluded)
        if decision.decision != EXCLUDED:  # op was excluded earlier; signals since changed. Still never filed.
            decision = type(decision)(EXCLUDED, ["previously_excluded"], {}, decision.policy_version,
                                      "EXCLUDED: previously excluded as a candidate interview; never filed")
        decision_id = self.store.record_decision(meeting, dest.key, decision)
        self._learn_candidates(meeting)
        in_folder = self._is_member_now(meeting, dest) or (op and op["status"] in ("verified", "already_member"))
        self.store.exclude(*key, decision_id)
        # Never removed automatically; a person decides.
        self.store.audit(self.actor, "candidate_interview_in_folder" if in_folder else "candidate_interview_excluded",
                         *key, outcome=",".join(decision.reasons))
        for r in related:  # other recordings of the same meeting get excluded too
            prior = self.store.latest_decision(r["provider"], r["source_id"])
            if not prior or prior["decision"] != EXCLUDED:
                self.store.enqueue(r["provider"], r["source_id"])
        log(f"{key[0]}:{key[1]} excluded reasons={','.join(decision.reasons)} in_folder={bool(in_folder)}")
        return "excluded"

    def _learn_candidates(self, meeting):
        """People outside Eagle/PLSA/LSVP on a typed candidate interview are candidates or recruiters."""
        if (meeting.meeting_type or "").strip().lower() not in self.s.policy.candidate_meeting_types:
            return
        ids = Identities(self.s.policy)
        emails = {e for e in participant_emails(meeting) if ids.kind(e) == "external"}
        new = self.store.add_candidate_identities(emails, f"{meeting.provider}:{meeting.source_id}")
        if new:
            for r in self.store.sources_with_participants(new):
                if (r["provider"], r["source_id"]) != (meeting.provider, meeting.source_id):
                    self.store.enqueue(r["provider"], r["source_id"])

    # ── helpers ──────────────────────────────────────────────────────────
    def _membership_readable(self, meeting, dest):
        return meeting.provider == "granola" and meeting.membership_known and bool(dest.rest_folder_id)

    def _is_member_now(self, meeting, dest):
        return self._membership_readable(meeting, dest) and dest.rest_folder_id.lower() in meeting.folder_ids

    def _review(self, meeting, dest, decision, decision_id, extra=(), op_status="review"):
        self.store.upsert_review(meeting, dest.key, decision, extra)
        self.store.set_op(meeting.provider, meeting.source_id, dest.key, op_status, decision_id)
        return op_status

    def check_destination(self, dest, writer, owner):
        """(problem, scope) or (None, None), cached briefly per owner.
        scope 'folder': the folder no longer matches the approved configuration, so all writes halt.
        scope 'owner': only this owner cannot file (not a member, viewer role)."""
        now = self.clock()
        cached = self._audience.get(owner)
        if cached and now - cached[0] < self.audience_ttl:
            return cached[1]
        folder = writer.get_folder(dest.folder_id)
        result = (None, None)
        if folder is None:
            result = ("destination_not_visible_to_owner", "owner")
        elif folder.title != dest.expected_title:
            result = ("destination_title_changed", "folder")
        elif folder.visibility != dest.approved_visibility:
            result = ("destination_visibility_changed", "folder")
        elif folder.audience != dest.approved_audience:
            result = ("destination_audience_changed", "folder")
        elif (folder.parent_folder_id or "") != (dest.approved_parent_folder_id or ""):
            result = ("destination_moved", "folder")
        elif folder.role not in ("owner", "collaborator"):
            result = ("owner_lacks_folder_role", "owner")
        else:
            for ep in self.granola.list_webhook_endpoints():
                fids = [str(x).lower() for x in ep.get("folder_ids") or []]
                if dest.rest_folder_id.lower() in fids and str(ep.get("id", "")).lower() not in \
                        self.s.granola_approved_webhook_ids:
                    result = ("unapproved_downstream_webhook", "folder")
                    break
        self._audience[owner] = (now, result)
        return result

    def _verify(self, meeting, dest):
        try:
            ids = self.granola.folder_ids_of(meeting.source_id)
        except ApiError as e:
            raise RetryLater(f"verify_{e.code}")
        return ids is not None and dest.rest_folder_id.lower() in ids

    def _write_granola(self, meeting, dest, decision, decision_id):
        key = (meeting.provider, meeting.source_id, dest.key)
        owner = meeting.owner_email
        writer = self.writer_for(owner) if owner in self.s.granola_writer_emails else None
        gate = None
        if self.store.writes_halted():
            gate = "writes_halted"
        elif not dest.rest_folder_id:
            gate = "folder_mapping_unverified"
        elif not meeting.membership_known:
            gate = "membership_unknown"
        elif owner not in self.s.granola_writer_emails:
            gate = "owner_authorization_needed"  # filed only with the owner's own login, never someone else's
        elif writer is None:
            gate = "writer_unconfigured"
        elif not self.s.granola_integrations_attestation:
            gate = "folder_integrations_unattested"
        if gate:
            return self._review(meeting, dest, decision, decision_id, ["auto_qualified", gate])

        try:
            problem, scope = self.check_destination(dest, writer, owner)
        except (TokenUnavailable, CapabilityUnavailable) as e:
            self.store.audit(self.actor, "owner_login_unavailable", *key, outcome=e.code)
            return self._review(meeting, dest, decision, decision_id, ["auto_qualified", "owner_login_unavailable"])
        except (ApiError, McpError) as e:
            raise RetryLater(f"audience_check_{e.code}")
        if problem and scope == "folder":
            self.store.halt_writes(problem)
            log(f"writes halted: {problem}")
            return self._review(meeting, dest, decision, decision_id, ["auto_qualified", problem])
        if problem:
            return self._review(meeting, dest, decision, decision_id, ["auto_qualified", problem])

        # Last check before any write, independent of the decision above.
        known = self.store.known_candidates(participant_emails(meeting))
        if exclusion_reasons(meeting, self.s.policy, known):
            self.store.exclude(*key, decision_id)
            return "excluded"
        prior = self.store.claim_op_for_write(*key, decision_id)
        if prior is None:
            return "op_held_elsewhere"
        try:
            if prior in ("uncertain", "unverified") and self._verify(meeting, dest):
                return self._verified(key, "verified_after_uncertain")
            result = writer.add(meeting.source_id, dest.folder_id)
        except WriteRejected as e:
            self.store.finish_op(*key, "blocked", e.code)
            self.store.audit(self.actor, "folder_add_rejected", *key, outcome=e.code)
            self.store.upsert_review(meeting, dest.key, decision, ["auto_qualified", "write_rejected", e.code])
            return "blocked"
        except WriteUncertain as e:
            self.store.finish_op(*key, "uncertain", e.code)
            self.store.audit(self.actor, "folder_add_uncertain", *key, outcome=e.code)
            raise RetryLater(e.code)
        except (TokenUnavailable, CapabilityUnavailable) as e:
            # One owner's login expired or was revoked: their notes wait; other owners keep filing.
            self.store.finish_op(*key, "failed_retryable", e.code)
            self.store.audit(self.actor, "owner_login_unavailable", *key, outcome=e.code)
            self.store.upsert_review(meeting, dest.key, decision, ["auto_qualified", "owner_login_unavailable"])
            return "owner_login_unavailable"
        except (ApiError, McpError, RetryLater) as e:
            keep = prior if prior in ("uncertain", "unverified") else "failed_retryable"
            self.store.finish_op(*key, keep, getattr(e, "code", type(e).__name__))
            raise RetryLater(getattr(e, "code", "retry"))

        self.store.audit(self.actor, "folder_add_sent", *key, outcome=result.outcome)
        try:
            member = self._verify(meeting, dest)
        except RetryLater:
            self.store.finish_op(*key, "unverified", "verify_failed")
            raise
        if member:
            return self._verified(key, f"verified_{result.outcome}")
        self.store.finish_op(*key, "unverified", "not_visible_yet")
        raise RetryLater("membership_not_verified")

    def _verified(self, key, outcome):
        self.store.finish_op(*key, "verified")
        self.store.audit(self.actor, "folder_add_verified", *key, outcome=outcome)
        self.store.supersede_review(*key)
        log(f"{key[0]}:{key[1]} filed and verified")
        return "verified"
