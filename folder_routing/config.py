import os
from dataclasses import dataclass, field

# Destinations supplied by the user. The Granola UUID is the folder's web-app ID
# (notes.granola.ai/f/<uuid>), which is the ID Granola MCP takes. The REST API
# uses separate fol_ IDs; the mapping must be verified, never derived.
GRANOLA_PLSA_FOLDER_UUID = "bae7dffb-3fd1-4de6-9a36-9a3fca2911bd"
FATHOM_PLSA_FOLDER_ID = "ZQa3o3WK22ysoSGPjDsP2y9dVaLmXcqTc1ab"

# Confirmed by the user on 2026-10-30 as PLSA-associated people using personal addresses.
DEFAULT_PLSA_ASSOCIATED_EMAILS = ("carruthers.bryan@gmail.com", "triciaesser@gmail.com", "zack.sikora6@gmail.com")

DEFAULT_SENSITIVE_TERMS = (
    "confidential", "privileged", "compensation", "comp", "bonus", "equity", "salary", "severance", "termination",
    "performance review", "offer letter", "legal", "lawsuit", "litigation", "board",
    "diligence", "qofe", "llca", "spa", "term sheet", "loi", "valuation", "people org", "hr",
)


def _bool(env, name, default=False):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(env, name, default=()):
    raw = env.get(name)
    if raw is None:
        return tuple(default)
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def _int(env, name, default):
    try:
        return int(env.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PolicyConfig:
    plsa_domains: tuple = ("plsaengineering.com",)
    plsa_emails: tuple = DEFAULT_PLSA_ASSOCIATED_EMAILS
    internal_domains: tuple = ("eagleeng.com",)
    investor_domains: tuple = ("lsvp.com", "opslsvp.com")
    ignorable_domains: tuple = ("resource.calendar.google.com", "group.calendar.google.com")
    sensitive_terms: tuple = DEFAULT_SENSITIVE_TERMS
    min_speaker_segments: int = 2
    granola_min_segments: int = 20           # remote call: total transcript segments
    granola_min_them_segments: int = 5       # remote call: segments from other people's audio
    granola_in_person_min_segments: int = 60 # no system audio at all: longer capture required


@dataclass(frozen=True)
class Destination:
    provider: str            # "fathom" | "granola"
    key: str                 # stable key used in the provider+source+destination unique constraint
    folder_id: str           # ID the provider's write interface takes (Granola MCP: folder UUID)
    folder_url: str
    expected_title: str = "PLSA"
    rest_folder_id: str = "" # Granola REST fol_ ID, pinned only after verify-granola-folder-mapping
    approved_visibility: str = ""
    approved_audience: str = ""
    approved_parent_folder_id: str = ""


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    writes_enabled: bool = False
    granola_writes_enabled: bool = False
    database_url: str = ""
    db_schema: str = "public"
    admin_token: str = ""
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    granola_dest: Destination = None
    fathom_dest: Destination = None
    granola_writer_emails: tuple = ()  # note owners who granted their own Granola login
    granola_integrations_attestation: str = ""
    granola_approved_webhook_ids: tuple = ()
    fathom_webhook_secret: str = ""
    fathom_api_key: str = ""
    granola_api_key: str = ""
    granola_mcp_url: str = "https://mcp.granola.ai/mcp"
    token_encryption_key: str = ""
    worker_poll_seconds: int = 15
    max_job_attempts: int = 6
    granola_sweep_seconds: int = 3600
    granola_sweep_overlap_minutes: int = 360
    fathom_sweep_seconds: int = 3600
    fathom_sweep_overlap_minutes: int = 360
    initial_lookback_minutes: int = 60

    def destination(self, provider):
        return self.granola_dest if provider == "granola" else self.fathom_dest

    def provider_writes_allowed(self, provider):
        # Fathom has no documented folder-write operation, so no flag can enable it.
        return provider == "granola" and self.enabled and self.writes_enabled and self.granola_writes_enabled


def load_settings(env=None):
    env = os.environ if env is None else env
    granola_uuid = env.get("GRANOLA_PLSA_FOLDER_UUID", GRANOLA_PLSA_FOLDER_UUID)
    fathom_folder = env.get("FATHOM_PLSA_FOLDER_ID", FATHOM_PLSA_FOLDER_ID)
    policy = PolicyConfig(
        plsa_domains=_list(env, "PLSA_DOMAINS", ("plsaengineering.com",)),
        plsa_emails=_list(env, "PLSA_VERIFIED_EMAILS", DEFAULT_PLSA_ASSOCIATED_EMAILS),
        internal_domains=_list(env, "INTERNAL_DOMAINS", (env.get("MY_DOMAIN", "eagleeng.com").lower(),)),
        investor_domains=_list(env, "INVESTOR_DOMAINS", ("lsvp.com", "opslsvp.com")),
        sensitive_terms=_list(env, "FOLDER_ROUTING_SENSITIVE_TERMS", DEFAULT_SENSITIVE_TERMS),
        min_speaker_segments=_int(env, "FOLDER_ROUTING_MIN_SPEAKER_SEGMENTS", 2),
        granola_min_segments=_int(env, "GRANOLA_MIN_SEGMENTS", 20),
        granola_min_them_segments=_int(env, "GRANOLA_MIN_THEM_SEGMENTS", 5),
        granola_in_person_min_segments=_int(env, "GRANOLA_IN_PERSON_MIN_SEGMENTS", 60),
    )
    return Settings(
        enabled=_bool(env, "FOLDER_ROUTING_ENABLED"),
        writes_enabled=_bool(env, "FOLDER_ROUTING_WRITES_ENABLED"),
        granola_writes_enabled=_bool(env, "GRANOLA_FOLDER_WRITES_ENABLED"),
        database_url=env.get("FOLDER_ROUTING_DATABASE_URL", ""),
        db_schema=env.get("FOLDER_ROUTING_DB_SCHEMA", "public"),
        admin_token=env.get("FOLDER_ROUTING_ADMIN_TOKEN", ""),
        policy=policy,
        granola_dest=Destination(
            provider="granola",
            key=f"granola:{granola_uuid}",
            folder_id=granola_uuid,
            folder_url=f"https://notes.granola.ai/f/{granola_uuid}",
            expected_title=env.get("GRANOLA_PLSA_FOLDER_TITLE", "PLSA"),
            rest_folder_id=env.get("GRANOLA_PLSA_FOLDER_REST_ID", ""),
            approved_visibility=env.get("GRANOLA_PLSA_APPROVED_VISIBILITY", "private"),
            approved_audience=env.get("GRANOLA_PLSA_APPROVED_AUDIENCE", "shared with other people"),
            approved_parent_folder_id=env.get("GRANOLA_PLSA_APPROVED_PARENT_ID", ""),
        ),
        fathom_dest=Destination(
            provider="fathom",
            key=f"fathom:{fathom_folder}",
            folder_id=fathom_folder,
            folder_url=f"https://fathom.video/folders/{fathom_folder}",
        ),
        granola_writer_emails=_list(env, "GRANOLA_WRITER_EMAILS", _list(env, "GRANOLA_WRITER_EMAIL")),
        granola_integrations_attestation=env.get("GRANOLA_FOLDER_INTEGRATIONS_ATTESTATION", "").strip(),
        granola_approved_webhook_ids=_list(env, "GRANOLA_APPROVED_WEBHOOK_ENDPOINT_IDS"),
        fathom_webhook_secret=env.get("FATHOM_WEBHOOK_SECRET", ""),
        fathom_api_key=env.get("FATHOM_API_KEY", ""),
        granola_api_key=env.get("GRANOLA_API_KEY", ""),
        granola_mcp_url=env.get("GRANOLA_MCP_URL", "https://mcp.granola.ai/mcp"),
        token_encryption_key=env.get("FOLDER_ROUTING_TOKEN_KEY", ""),
        worker_poll_seconds=_int(env, "FOLDER_ROUTING_POLL_SECONDS", 15),
        max_job_attempts=_int(env, "FOLDER_ROUTING_MAX_ATTEMPTS", 6),
        granola_sweep_seconds=_int(env, "FOLDER_ROUTING_GRANOLA_SWEEP_SECONDS", 3600),
        granola_sweep_overlap_minutes=_int(env, "FOLDER_ROUTING_GRANOLA_SWEEP_OVERLAP_MINUTES", 360),
        fathom_sweep_seconds=_int(env, "FOLDER_ROUTING_FATHOM_SWEEP_SECONDS", 3600),
        fathom_sweep_overlap_minutes=_int(env, "FOLDER_ROUTING_FATHOM_SWEEP_OVERLAP_MINUTES", 360),
        initial_lookback_minutes=_int(env, "FOLDER_ROUTING_INITIAL_LOOKBACK_MINUTES", 60),
    )
