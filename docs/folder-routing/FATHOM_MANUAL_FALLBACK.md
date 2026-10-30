# Manual filing for Fathom

Fathom's API has no folder operations, so the app classifies Fathom calls but never files them. Every Fathom call that qualifies (or needs a judgment call) lands in the review queue with its link.

Reason codes on Fathom queue items:
- `auto_qualified` + `manual_filing_required`: policy says file it.
- anything else (`plsa_invitee_only`, `third_party_present`, ...): judgment call.

## Daily routine

1. List open items (token kept in an env var, never in a URL):

```bash
curl -s -H "Authorization: Bearer $FOLDER_ROUTING_ADMIN_TOKEN" "$APP_URL/admin/folder-routing/review" | python3 -m json.tool
```

2. For each Fathom item you decide to file: open its `link`, add the call to the PLSA folder (https://fathom.video/folders/ZQa3o3WK22ysoSGPjDsP2y9dVaLmXcqTc1ab) in the Fathom app, and confirm it shows in the folder.

3. Record the outcome (`filed_manually` or `rejected`):

```bash
curl -s -X POST -H "Authorization: Bearer $FOLDER_ROUTING_ADMIN_TOKEN" -H "Content-Type: application/json" -d '{"status":"filed_manually","note":"added in Fathom UI"}' "$APP_URL/admin/folder-routing/review/<id>"
```

Resolved items are never reopened by later events. The same call arriving again (webhook retry, sweep) updates nothing.

## Before filing, check

- Not already in the folder (the app cannot see Fathom folder membership).
- No other recording of the same meeting already in the folder (`possible_duplicate_recording`).
- The call's audience in Fathom is compatible with the folder's audience. Adding to a folder can widen who sees the call.
