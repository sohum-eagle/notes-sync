# Classification policy and proposed audience rules

Policy version: `plsa-attendance-v2` (`folder_routing/policy.py`). Every decision is stored in `fr_decisions` with its reasons, evidence (emails, domains, counts; never content) and policy version.

## Inputs

Identities, calendar invitees, Granola attendee lists, note owner / Fathom recorder, and speaker metadata (Fathom: provider-matched email and segment count; Granola: `me`/`them` counts). Transcript text and summaries are never stored, logged or interpreted. No model is called, so meeting content cannot steer a decision.

Identity classes: `plsa` (domain `plsaengineering.com` or an address in `PLSA_VERIFIED_EMAILS`, default: carruthers.bryan@gmail.com, triciaesser@gmail.com, zack.sikora6@gmail.com), `internal` (`eagleeng.com`), `investor` (`lsvp.com`, `opslsvp.com`), `ignore` (Google resource calendars), `external` (everything else, including personal Gmail).

## Rules

| Signal | Strength | Meaning |
|---|---|---|
| `plsa_speaker_verified` | strong | Fathom matched a speaker to a PLSA email and they spoke at least 2 segments |
| `plsa_note_owner` | strong | A PLSA identity captured the Granola note |
| `plsa_attendance_corroborated` | strong | A Fathom recording with the same UTC start minute and identical invitee set has `plsa_speaker_verified` |
| `plsa_meeting_evident` | strong | Granola note captured by an Eagle person; PLSA-affiliated people are at least half of everyone else invited; a conversation was captured (remote: 20+ segments with 5+ from other people's audio; in person, no system audio: 60+ segments) |
| `plsa_recorder`, `plsa_speaker_name_only`, `plsa_speaker_below_min_segments` | medium | Review |
| `plsa_invitee_only`, `plsa_attendee_listed`, `plsa_title_mention` | weak | Review (calendar invitees and titles are not attendance) |
| `third_party_present`, `investor_present`, `sensitive_title`, `possible_duplicate_recording` | blocker | Forces review even with a strong signal |

Outcome: **auto_file** only with a strong signal and no blocker. **review** with any PLSA signal. **skip** otherwise; Eagle/LSVP-only calls are labelled `internal_or_investor_only` and never auto-filed.

Already-filed items are recorded as `already_member` and never reclassified or changed, so historical exceptions stay. If a person later removes a filed item, the system does not re-add it.

Sensitive title terms (`FOLDER_ROUTING_SENSITIVE_TERMS`): confidential, privileged, compensation, comp, bonus, equity, salary, severance, termination, performance review, offer letter, legal, lawsuit, litigation, board, diligence, qofe, llca, spa, term sheet, loi, valuation, people org, hr. Whole-word match.

## Audience rules

Writes stop globally (`fr_controls.writes_halted`) until an admin resumes them if the folder itself changes:

1. Destination is Granola folder `bae7dffb-3fd1-4de6-9a36-9a3fca2911bd` with title `PLSA`, `visibility: private`, `audience: "shared with other people"`, top level.
2. No Granola webhook endpoint visible to the API key is scoped to the folder unless listed in `GRANOLA_APPROVED_WEBHOOK_ENDPOINT_IDS`.
3. `GRANOLA_FOLDER_INTEGRATIONS_ATTESTATION` is set after someone checks the folder's Integrations panel. The API cannot see these.

Per-owner rules (only that owner's notes wait; nothing halts):

4. A note is filed only with its owner's own Granola login. Teammates' notes are never filed with another person's login. Owners without a login get review items tagged with their email.
5. The owner must be an owner or collaborator on the folder.
6. Participants must be PLSA-affiliated and Eagle only. Any investor, advisor, lawyer or unknown address forces review.

Known gap: MCP reports the audience as a phrase, not a member list. Adding a person to the folder does not change any field the code can read.

## Decisions recorded (2026-10-30)

1. carruthers.bryan@gmail.com, triciaesser@gmail.com, zack.sikora6@gmail.com count as PLSA-associated.
2. Teammates' notes are filed if applicable, using each teammate's own login, never the folder owner's.
3. The folder has no Attio auto-post; Attio duplication is not a concern.
4. Granola notes are filed when it is clearly evident the meeting was with PLSA-affiliated people (`plsa_meeting_evident`).

Still review-only by default: investors or other outside parties present, sensitive titles, duplicate recordings, Eagle-majority meetings.
