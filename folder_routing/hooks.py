"""Entry points called from the existing ingestion paths.

Both are no-ops unless FOLDER_ROUTING_ENABLED is set, never raise, and do only
a fast enqueue: classification and folder writes happen in the background
worker, so Attio sync is unaffected by routing failures and vice versa.
"""
from . import log, runtime, signatures
from .normalize import fathom_meeting_to_source


def on_fathom_webhook(headers, raw_body, payload):
    try:
        s = runtime.settings()
        if not s.enabled:
            return "disabled"
        # Documented Standard Webhooks scheme (webhook-id/-timestamp/-signature).
        # Unverified deliveries are ignored here; the Fathom sweep still finds the
        # recording through the authenticated API if FATHOM_API_KEY is set.
        if not signatures.verify(s.fathom_webhook_secret, headers, raw_body):
            log("fathom webhook not verified; not enqueued")
            return "unverified"
        if not isinstance(payload, dict) or payload.get("recording_id") is None:
            return "no_recording_id"
        meeting = fathom_meeting_to_source(payload)
        st = runtime.store()
        if not st.record_delivery("fathom", signatures.delivery_id(headers), meeting.source_id):
            return "duplicate_delivery"
        st.upsert_source(meeting)
        st.enqueue("fathom", meeting.source_id)
        return "enqueued"
    except Exception as e:
        log(f"fathom hook error={type(e).__name__}")
        return "error"


def on_granola_notes_listed(notes):
    try:
        if not runtime.settings().enabled:
            return 0
        st = runtime.store()
        n = 0
        for note in notes or []:
            note_id = str((note or {}).get("id") or "")
            if note_id.lower().startswith("not_"):
                st.enqueue("granola", note_id)
                n += 1
        return n
    except Exception as e:
        log(f"granola hook error={type(e).__name__}")
        return 0
