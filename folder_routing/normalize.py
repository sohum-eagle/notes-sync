"""Provider payloads -> SourceMeeting.

Only metadata survives normalization. Transcript text and summaries are read
for speaker counts and then dropped; they are never stored, logged or
interpreted. Transcript content is untrusted data, not instructions.
"""
import re
from collections import OrderedDict

from .models import SourceMeeting, Participant, SpeakerEvidence, clean_email

GRANOLA_WEB_URL_RE = re.compile(
    r"^https://notes\.granola\.ai/d/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$"
)


def _participants(items, email_key="email", name_keys=("name",)):
    out, seen = [], set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        email = clean_email(item.get(email_key))
        if not email or email in seen:
            continue
        seen.add(email)
        name = next((str(item.get(k) or "").strip() for k in name_keys if item.get(k)), "")
        out.append(Participant(email=email, name="" if "@" in name else name))
    return out


def fathom_speakers(transcript):
    counts = OrderedDict()
    for item in transcript or []:
        speaker = (item or {}).get("speaker") or {}
        key = (str(speaker.get("display_name") or "").strip(),
               clean_email(speaker.get("matched_calendar_invitee_email")))
        counts[key] = counts.get(key, 0) + 1
    return [SpeakerEvidence(display_name=n, matched_email=e, segments=c) for (n, e), c in counts.items()]


def fathom_meeting_to_source(meeting, transcript=None):
    """Webhook payload or GET /meetings item. `transcript` overrides meeting['transcript']."""
    recording_id = meeting.get("recording_id")
    if recording_id is None:
        raise ValueError("fathom meeting without recording_id")
    items = transcript if transcript is not None else meeting.get("transcript")
    recorder = meeting.get("recorded_by") or {}
    return SourceMeeting(
        provider="fathom",
        source_id=str(int(recording_id)),
        title=str(meeting.get("meeting_title") or meeting.get("title") or ""),
        link=str(meeting.get("url") or meeting.get("share_url") or ""),
        owner_email=clean_email(recorder.get("email")),
        scheduled_start=str(meeting.get("scheduled_start_time") or ""),
        created_at=str(meeting.get("created_at") or ""),
        meeting_url=str(meeting.get("meeting_url") or ""),
        invitees=_participants(meeting.get("calendar_invitees")),
        speakers=fathom_speakers(items) if items else [],
        transcript_available=bool(items),
        shared_with=str(meeting.get("shared_with") or ""),
        meeting_type=str(meeting.get("meeting_type") or ""),
        membership_known=False,  # Fathom's API exposes no folder data
        alt_ids={"share_url": str(meeting.get("share_url") or "")},
    )


def granola_speakers(transcript):
    counts = OrderedDict()
    for item in transcript or []:
        speaker = (item or {}).get("speaker") or {}
        key = (str(speaker.get("attribution") or "unknown"), str(speaker.get("source") or "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return [SpeakerEvidence(attribution=a, source=src, segments=c) for (a, src), c in counts.items()]


def granola_uuid_from_web_url(web_url):
    """Read the note UUID from the documented web_url field. Returns '' if the shape differs."""
    m = GRANOLA_WEB_URL_RE.match(str(web_url or ""))
    return m.group(1) if m else ""


def granola_note_to_source(note, transcript=None):
    """GET /v1/notes/{id} response. `transcript` overrides note['transcript']."""
    note_id = str(note.get("id") or "")
    if not note_id.lower().startswith("not_"):
        raise ValueError("granola note without a not_ ID")
    cal = note.get("calendar_event") or {}
    items = transcript if transcript is not None else note.get("transcript")
    folders = note.get("folder_membership")
    return SourceMeeting(
        provider="granola",
        source_id=note_id,
        title=str(note.get("title") or cal.get("event_title") or ""),
        link=str(note.get("web_url") or ""),
        owner_email=clean_email((note.get("owner") or {}).get("email")),
        scheduled_start=str(cal.get("scheduled_start_time") or ""),
        created_at=str(note.get("created_at") or ""),
        updated_at=str(note.get("updated_at") or ""),
        calendar_event_id=str(cal.get("calendar_event_id") or ""),
        invitees=_participants(cal.get("invitees")),
        attendees=_participants(note.get("attendees")),
        speakers=granola_speakers(items) if items else [],
        transcript_available=bool(items),
        folder_ids=[str(f.get("id")).lower() for f in folders or [] if isinstance(f, dict) and f.get("id")],
        membership_known=isinstance(folders, list),
        alt_ids={"uuid": granola_uuid_from_web_url(note.get("web_url"))},
    )
