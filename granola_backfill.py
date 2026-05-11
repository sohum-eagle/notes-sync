"""Run once to backfill all past Granola notes into Attio."""
import os
from granola_sync import sync

if __name__ == "__main__":
    # Use a far-past date to fetch everything
    print("Starting Granola backfill (all notes)...")
    done, skipped = sync(created_after="2020-01-01T00:00:00Z")
    print(f"\nBackfill complete: {done} notes created, {skipped} skipped.")
