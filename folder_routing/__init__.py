"""Feature-flagged folder routing for Fathom recordings and Granola notes.

Runs beside the Attio sync, never inside it. Everything is off unless
FOLDER_ROUTING_ENABLED is set, and folder writes need further explicit flags.
See docs/folder-routing/ for the capability report and configuration.
"""


def log(msg):
    # Matches the app's print-based logging. Callers pass IDs and reason codes
    # only: never titles, emails, summaries or transcript text.
    print(f"[folder-routing] {msg}", flush=True)
