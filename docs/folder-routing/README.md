# Folder routing

Classifies Fathom recordings and Granola notes for the PLSA folders and files qualifying Granola notes. Separate from the Attio sync: it shares only the entry points (Fathom webhook, Granola poll), runs in a background worker, and neither path's failures affect the other.

- `CAPABILITY_REPORT.md`: what each provider supports and what is blocked
- `POLICY.md`: classification rules, proposed audience rules, open decisions
- `FATHOM_MANUAL_FALLBACK.md`: filing Fathom calls by hand

## Flow

```
Fathom webhook ─(signature ok, new webhook-id)─┐
Fathom sweep (FATHOM_API_KEY, hourly) ─────────┼─> fr_jobs (unique provider+source) ─> worker
Granola poll (existing, 15 min) ───────────────┤      claimed with SKIP LOCKED
Granola sweep (hourly, 6h overlap) ────────────┘
worker: load -> existing membership? -> classify -> duplicates -> write gates -> CAS claim -> add -> verify
```

All state is in Postgres (`fr_*` tables): jobs, decisions, folder ops, review queue, checkpoints, leases, controls, encrypted OAuth tokens, append-only audit log. The `/tmp` dedup files are not used.

Idempotency key: `(provider, source_id, destination)` on `fr_folder_ops`. Only one worker can move a key to `writing`; terminal states (`verified`, `already_member`, `blocked`) are never overwritten. A write that may have landed is `uncertain` and is verified before any retry. Success is recorded only after membership is read back.

## Configuration

All off by default. With no flags set, the only visible changes are: `/last-webhook` returns 404 without the admin token, and `/admin/folder-routing/*` returns 404.

| Variable | Default | Purpose |
|---|---|---|
| `FOLDER_ROUTING_ENABLED` | off | Classification, review queue, worker |
| `FOLDER_ROUTING_DATABASE_URL` | unset | Postgres DSN (required when enabled) |
| `FOLDER_ROUTING_DB_SCHEMA` | `public` | Schema for `fr_*` tables |
| `FOLDER_ROUTING_ADMIN_TOKEN` | unset | Bearer token for admin endpoints and `/last-webhook` |
| `FOLDER_ROUTING_WRITES_ENABLED` | off | Global write switch |
| `GRANOLA_FOLDER_WRITES_ENABLED` | off | Granola write switch (both needed) |
| `GRANOLA_PLSA_FOLDER_REST_ID` | unset | `fol_` ID, set only after `verify-granola-folder-mapping` passes |
| `GRANOLA_WRITER_EMAIL` | unset | Only notes owned by this account are filed |
| `GRANOLA_FOLDER_INTEGRATIONS_ATTESTATION` | unset | Who checked the folder's integrations, and when |
| `GRANOLA_APPROVED_WEBHOOK_ENDPOINT_IDS` | empty | Folder-scoped Granola webhooks that are allowed |
| `FOLDER_ROUTING_TOKEN_KEY` | unset | Fernet key for the stored MCP token |
| `FATHOM_WEBHOOK_SECRET` | existing | Must be the `whsec_` value for routing to accept webhooks |
| `FATHOM_API_KEY` | existing in scripts | Optional: sweep and transcript fetch |
| `GRANOLA_API_KEY` | existing | Needs the personal scope to read the private PLSA folder |
| `PLSA_DOMAINS`, `PLSA_VERIFIED_EMAILS`, `INTERNAL_DOMAINS`, `INVESTOR_DOMAINS`, `FOLDER_ROUTING_SENSITIVE_TERMS`, `FOLDER_ROUTING_MIN_SPEAKER_SEGMENTS` | see `config.py` | Policy inputs |
| `FOLDER_ROUTING_*_SWEEP_SECONDS`, `*_OVERLAP_MINUTES`, `FOLDER_ROUTING_INITIAL_LOOKBACK_MINUTES`, `FOLDER_ROUTING_MAX_ATTEMPTS`, `FOLDER_ROUTING_POLL_SECONDS` | see `config.py` | Worker tuning. First sweep looks back 60 minutes: no historical backfill |

## Permissions needed (none granted yet)

| What | Who | Why |
|---|---|---|
| Postgres on the notes-sync Railway project, `FOLDER_ROUTING_DATABASE_URL` set as a reference variable | Railway project admin | Durable jobs, dedup, audit |
| `FOLDER_ROUTING_ADMIN_TOKEN`, `FOLDER_ROUTING_TOKEN_KEY` | Railway project admin | Admin endpoints, token encryption |
| Confirm `FATHOM_WEBHOOK_SECRET` is the `whsec_` value of the existing webhook | Fathom webhook owner | Signature verification |
| Granola API key with personal scope (confirm the existing key) | Folder owner | Read PLSA folder membership |
| One-time Granola MCP OAuth grant as the folder owner | Folder owner, interactively | Only path to `add_note_to_folder` |
| Check of the folder's Integrations panel | Folder owner | Downstream sharing gate |

No new Fathom or Granola webhooks are registered by this change.

## Rollout

1. Deploy with all flags off. Attio behavior is unchanged.
2. Provision Postgres, set `FOLDER_ROUTING_DATABASE_URL`, `FOLDER_ROUTING_ADMIN_TOKEN`, `FOLDER_ROUTING_ENABLED=1`. Classification and review queue only.
3. `python -m folder_routing.cli capability-check`, then `verify-granola-folder-mapping --rest-id fol_...`; pin `GRANOLA_PLSA_FOLDER_REST_ID` only on PASS.
4. Watch the review queue for two weeks; tune `PLSA_VERIFIED_EMAILS` and terms.
5. With approval: `granola-oauth-login`, set writer email, attestation, `FOLDER_ROUTING_TOKEN_KEY`, then both write flags. `requeue --status dry_run` replays items that qualified while writes were off.
6. Historical backfill: not automatic. `granola_backfill.py` deliberately does not trigger routing. Run `cli dry-run --since ... --out report.md` first and get approval.

Kill switch: unset `FOLDER_ROUTING_WRITES_ENABLED`, or let any audience check trip `writes_halted`; resume with `POST /admin/folder-routing/resume-writes` or `cli resume-writes --reason ...`.

## Tests

```bash
pip install -r requirements.txt pytest
```

```bash
FOLDER_ROUTING_TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/scratch_db pytest tests
```

Each test uses a throwaway schema. Store, pipeline and app tests skip without the variable.
