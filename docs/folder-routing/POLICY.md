# Classification policy and proposed audience rules

Policy version: `plsa-attendance-v1` (`folder_routing/policy.py`). Every decision is stored in `fr_decisions` with its reasons, evidence (emails, domains, counts; never content) and policy version.

## Inputs

Identities, calendar invitees, Granola attendee lists, note owner / Fathom recorder, and speaker metadata (Fathom: provider-matched email and segment count; Granola: `me`/`them` counts). Transcript text and summaries are never stored, logged or interpreted. No model is called, so meeting content cannot steer a decision.

Identity classes: `plsa` (domain `plsaengineering.com` or an address in `PLSA_VERIFIED_EMAILS`), `internal` (`eagleeng.com`), `investor` (`lsvp.com`, `opslsvp.com`), `ignore` (Google resource calendars), `external` (everything else, including personal Gmail).

## Rules

| Signal | Strength | Meaning |
|---|---|---|
| `plsa_speaker_verified` | strong | Fathom matched a speaker to a PLSA email and they spoke at least 2 segments |
| `plsa_note_owner` | strong | A PLSA identity captured the Granola note |
| `plsa_attendance_corroborated` | strong | A Fathom recording with the same UTC start minute and identical invitee set has `plsa_speaker_verified` |
| `plsa_recorder`, `plsa_speaker_name_only`, `plsa_speaker_below_min_segments` | medium | Review |
| `plsa_invitee_only`, `plsa_attendee_listed`, `plsa_title_mention` | weak | Review (calendar invitees and titles are not attendance) |
| `third_party_present`, `investor_present`, `sensitive_title`, `possible_duplicate_recording` | blocker | Forces review even with a strong signal |

Outcome: **auto_file** only with a strong signal and no blocker. **review** with any PLSA signal. **skip** otherwise; Eagle/LSVP-only calls are labelled `internal_or_investor_only` and never auto-filed.

Already-filed items are recorded as `already_member` and never reclassified or changed, so historical exceptions stay. If a person later removes a filed item, the system does not re-add it.

Sensitive title terms (`FOLDER_ROUTING_SENSITIVE_TERMS`): confidential, privileged, compensation, comp, bonus, equity, salary, severance, termination, performance review, offer letter, legal, lawsuit, litigation, board, diligence, qofe, llca, spa, term sheet, loi, valuation, people org, hr. Whole-word match.

## Proposed audience rules (need your approval before any write)

Writes stop globally (`fr_controls.writes_halted`) and stay stopped until an admin resumes them if any check fails:

1. Destination is Granola folder `bae7dffb-3fd1-4de6-9a36-9a3fca2911bd` with title `PLSA`, `visibility: private`, `audience: "shared with other people"`, top level.
2. The writer's role on the folder is `owner` or `collaborator`.
3. No Granola webhook endpoint visible to the API key is scoped to the folder unless listed in `GRANOLA_APPROVED_WEBHOOK_ENDPOINT_IDS`.
4. `GRANOLA_FOLDER_INTEGRATIONS_ATTESTATION` is set after someone checks the folder's Integrations panel (Slack, HubSpot, Attio, Zapier) by hand. The API cannot see these.
5. Only notes owned by `GRANOLA_WRITER_EMAIL` are filed. Teammates' notes go to review, even where MCP would allow a collaborator to add them.
6. Only notes whose participants are PLSA and Eagle. Any investor, advisor, lawyer or unknown address forces review.

Known gap: MCP reports the audience as a phrase, not a member list. Adding a person to the folder does not change any field the code can read.

## Decisions for you

1. Are `carruthers.bryan@gmail.com`, `triciaesser@gmail.com` and `zack.sikora6@gmail.com` PLSA identities? They appear on PLSA calls; today they count as unknown third parties. Add confirmed ones to `PLSA_VERIFIED_EMAILS`.
2. Should teammates' notes (Danny, Mayank, Arman) ever be filed by the folder owner's token? Default: no.
3. Does the PLSA Granola folder have an Attio, Slack, HubSpot or Zapier integration? If Attio, filing duplicates CRM notes.
4. Who exactly is "shared with other people" on the folder? Confirm it is the intended audience.
5. Granola transcripts never identify speakers, so Sohum-owned Granola notes auto-file only when a Fathom recording of the same slot corroborates PLSA attendance. Keep this, or accept review-only for Granola?
