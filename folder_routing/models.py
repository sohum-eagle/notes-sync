import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(value):
    email = (value or "").strip().lower()
    return email if EMAIL_RE.match(email) else ""


def email_domain(email):
    return email.rsplit("@", 1)[1] if "@" in email else ""


@dataclass
class Participant:
    email: str
    name: str = ""


@dataclass
class SpeakerEvidence:
    """Aggregated speaker metadata. Holds no transcript text."""
    display_name: str = ""
    matched_email: str = ""
    attribution: str = ""   # Granola: "me" / "them"; Fathom: ""
    source: str = ""        # Granola: "microphone" / "speaker" (system audio); Fathom: ""
    segments: int = 0


@dataclass
class SourceMeeting:
    provider: str                 # "fathom" | "granola"
    source_id: str                # immutable: Fathom recording_id, Granola not_ ID
    title: str = ""               # display only; never used for dedup
    link: str = ""
    owner_email: str = ""         # Fathom recorded_by, Granola note owner
    scheduled_start: str = ""
    created_at: str = ""
    updated_at: str = ""
    calendar_event_id: str = ""
    meeting_url: str = ""
    invitees: list = field(default_factory=list)    # [Participant] from the calendar
    attendees: list = field(default_factory=list)   # [Participant] Granola attendee list (not proof)
    speakers: list = field(default_factory=list)    # [SpeakerEvidence]
    transcript_available: bool = False
    folder_ids: list = field(default_factory=list)  # current memberships, provider REST IDs
    membership_known: bool = False
    shared_with: str = ""
    alt_ids: dict = field(default_factory=dict)

    def snapshot(self):
        return asdict(self)

    @classmethod
    def from_snapshot(cls, data):
        data = dict(data)
        data["invitees"] = [Participant(**p) for p in data.get("invitees", [])]
        data["attendees"] = [Participant(**p) for p in data.get("attendees", [])]
        data["speakers"] = [SpeakerEvidence(**s) for s in data.get("speakers", [])]
        return cls(**data)

    def start_minute_utc(self):
        try:
            dt = datetime.fromisoformat(self.scheduled_start.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return ""
        if dt.tzinfo is None:
            return ""
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")

    def fingerprint(self):
        """Same-provider identity of the underlying meeting (not of the recording)."""
        if self.calendar_event_id:
            return f"cal:{self.calendar_event_id}"
        minute = self.start_minute_utc()
        if self.meeting_url and minute:
            return f"url:{self.meeting_url.split('?')[0].lower()}|{minute}"
        return ""

    def slot_fingerprint(self):
        """Cross-provider match: same UTC start minute and identical invitee email set."""
        emails = sorted({p.email for p in self.invitees if p.email})
        minute = self.start_minute_utc()
        if not minute or not emails:
            return ""
        digest = hashlib.sha256(json.dumps(emails).encode()).hexdigest()[:16]
        return f"slot:{minute}|{digest}"


@dataclass
class Decision:
    decision: str                 # "auto_file" | "review" | "skip"
    reasons: list
    evidence: dict
    policy_version: str
    explanation: str = ""
