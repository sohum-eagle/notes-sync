from dataclasses import dataclass


class CapabilityUnavailable(Exception):
    """The provider documents no supported interface for this operation."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class WriteRejected(Exception):
    """Permanent refusal (read-only note, no permission, not found). Never retried."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class WriteUncertain(Exception):
    """The request may or may not have been applied (timeout after send). Verify before retrying."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class FolderInfo:
    folder_id: str
    title: str
    visibility: str
    audience: str
    parent_folder_id: str
    role: str


@dataclass
class WriteResult:
    outcome: str              # "added" | "already_in_folder" | "unknown"
    folder: FolderInfo = None
