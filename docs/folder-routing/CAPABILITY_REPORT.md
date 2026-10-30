# Folder routing: capability report

Checked 2026-10-30 against the providers' published specs and the tools reachable from this build session. Nothing was written to Fathom, Granola, Attio or Railway.

Sources:
- Fathom OpenAPI: https://developers.fathom.ai/api-reference/openapi.yaml, webhooks: https://developers.fathom.ai/webhooks, FAQ: https://developers.fathom.ai/faq
- Granola OpenAPI: https://docs.granola.ai/api-reference/openapi.json, webhooks: https://docs.granola.ai/webhooks, MCP: https://docs.granola.ai/help-center/sharing/integrations/mcp, API keys: https://docs.granola.ai/help-center/sharing/integrations/granola-api, folders: https://docs.granola.ai/help-center/sharing/folders/spaces-and-folders
- Granola MCP OAuth metadata: https://mcp.granola.ai/.well-known/oauth-protected-resource, https://mcp-auth.granola.ai/.well-known/oauth-authorization-server
- Zapier Fathom app: https://zapier.com/apps/fathom/integrations

## Summary

| Operation | Fathom | Granola |
|---|---|---|
| Discover meetings | Supported (REST `GET /meetings`, webhook) | Supported (REST `GET /v1/notes`, existing poll) |
| Attendance evidence | Supported: `transcript[].speaker.matched_calendar_invitee_email` | Weak: transcript speakers are only `me`/`them`, never identified |
| Read folder membership | **Blocked**: no folder endpoints | Supported: `GET /v1/notes/{id}` returns `folder_membership` (`fol_` IDs) |
| Read folder audience | **Blocked** | Supported via MCP `list_meeting_folders` (`visibility`, `audience`, `role`) |
| Add item to folder | **Blocked**: no documented operation | MCP `add_note_to_folder` exists; **blocked for unattended use** (see below) |
| Remove / move / copy | Not used | Not used (`remove_note_from_folder` exists; this code never calls it) |

## Fathom

- The OpenAPI spec has 11 paths: `/meetings`, `/recordings/{id}/summary`, `/recordings/{id}/transcript`, `/recordings/{id}/download`, `/recordings/{id}/downloads/{download_id}`, `/teams`, `/team_members`, `/users`, `/meeting_types`, `/webhooks`, `/webhooks/{id}`. None reads or writes folders; the word "folder" appears nowhere in the API docs.
- The Fathom MCP connector says "All tools are read-only" and has no folder tools.
- Zapier's Fathom app has 6 triggers and 0 actions.
- Result: writer hard-disabled (`FathomFolderWriter` raises `CapabilityUnavailable`; no flag enables it). Qualifying calls go to the review queue with the call link. See `FATHOM_MANUAL_FALLBACK.md`. No browser cookies, no undocumented endpoints.
- IDs: `recording_id` (integer) is the immutable key. It differs from the number in `fathom.video/calls/<n>` URLs.
- Webhooks are signed with Standard Webhooks headers (`webhook-id`, `webhook-timestamp`, `webhook-signature`, secret `whsec_...`). Retries reuse `webhook-id` (FAQ), which is the delivery dedup key.

## Granola

- REST has 19 endpoints; none writes folders. Reads: notes (cursor, `updated_after`, `folder_id`), note detail with `folder_membership`, paginated transcript, folders (`fol_` IDs, names, parent, space; no visibility/audience), spaces, webhook endpoints.
- MCP `add_note_to_folder` is live in this session's connector and documented in its tool description as "additive and idempotent", allowed only for notes the user created or collaborates on, and it "grants everyone with access to the folder access to the note". Granola's public MCP doc table does not list it yet.
- MCP auth: unauthenticated `POST https://mcp.granola.ai/mcp` returns `401` with `resource_metadata`. Granola documents browser OAuth with dynamic client registration and Enterprise-Managed Authorization; it documents no API-key access and no unattended server use. The authorization server advertises `refresh_token`, `offline_access`, `device_code` and `jwt-bearer`, so a one-time interactive grant with refresh is technically possible, but unverified for this use. The code never sends a REST API key to MCP.
- **Blocker for writes**: a person (the folder owner) must run `python -m folder_routing.cli granola-oauth-login` once. Tokens are stored encrypted in Postgres. If refresh is refused, writes halt and items queue for review.
- IDs: REST notes are `not_` + 14 chars, folders `fol_` + 14 chars. Web/MCP use UUIDs. `add_note_to_folder` accepts a `not_` ID directly, so notes need no conversion. The folder's `fol_` ID is pinned only after `verify-granola-folder-mapping` shows the REST folder's members (UUIDs read from each note's documented `web_url`) equal the MCP folder listing. No ID is derived or converted.
- Webhooks: `note.generated`, `note.edited`, `note.access_granted`, Standard Webhooks signing, optional folder filter, Business/Enterprise. Useful later to replace polling; not registered (needs approval).
- Downstream sharing: folders can auto-post added notes to Slack, HubSpot, Attio and Zapier. These folder integrations are not exposed by REST or MCP. Writes therefore require a manual attestation (`GRANOLA_FOLDER_INTEGRATIONS_ATTESTATION`). If an Attio auto-post is enabled on the PLSA folder, filing would create Attio notes in addition to notes-sync's own.

### Live read-only observations (this session's connectors, not the app's credentials)

- Granola account `sohum@eagleeng.com`, workspace Eagle, MCP plan `plus`, scopes `personal` + `public`.
- PLSA folder `bae7dffb-3fd1-4de6-9a36-9a3fca2911bd`: `visibility: private`, `audience: "shared with other people"`, top level, role `owner`, 34 notes. MCP does not list the folder's members, so adding a person to the folder cannot be detected; only visibility, audience text, title, parent and role changes can.
- Folder history includes Eagle-only notes ("90 day", "Danny X Mike") and an LSVP/AlphaSights call: the historical exceptions to preserve. Many notes are owned by Danny, Mayank or Arman, not the folder owner.
- Fathom identity `sohum@eagleeng.com`; teams Engineering, Executive.

## Railway

- `RAILWAY_TOKEN` in this environment is invalid. `RAILWAY_TOKEN_2` is a valid project token for a different project (`lift-tracker`: services `lift-tracker`, `Postgres`). It has no access to notes-sync. I did not touch that project.
- **The notes-sync service mapping is unverified.** Run these with a notes-sync project token (they print names, not values):

```bash
railway status --json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["name"],[s["node"]["name"] for s in d["services"]["edges"]])'
```

```bash
railway variables --json | python3 -c 'import json,sys;print(sorted(json.load(sys.stdin)))'
```

- The feature needs Postgres. Confirm whether the notes-sync project already has one before adding it.

## Existing-code findings

| Finding | Changed here? |
|---|---|
| `/webhook` checks `x-fathom-signature`, which Fathom does not document, and skips verification when it is absent, so the Attio path accepts unsigned requests. | No (Attio path untouched). Routing verifies the documented scheme. Recommend switching after confirming the secret is `whsec_`. |
| `/last-webhook` returned the last raw payload (summary, transcript, invitees) to anyone. | **Yes**: admin token required, 404 otherwise. |
| `/summary-notes`, `/latest-email`, `/last-meeting` are unauthenticated; `/summary-notes` returns AI summaries of meeting notes for any domain. Likely used by Clay. | No. Needs a decision. |
| Logs print meeting titles and a partial HMAC. | No. Routing logs IDs and reason codes only. |
| Attio dedup compares note titles, so two different calls with the same title for the same person/company are dropped. | No. Routing dedups by source ID. |
