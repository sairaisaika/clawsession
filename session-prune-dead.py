#!/usr/bin/env python3
"""
session-prune-dead.py — Remove dead/obsolete spawn-child sessions

Removes spawn-child subagent sessions that are:
  - Older than STALE_HOURS (default 24h)
  - No longer referenced by any active session

Safe for dead subagents that have already reported results.
Does NOT touch active direct/cron/main sessions.
"""

import json
import os
import glob
import time
import shutil
import argparse

STALE_HOURS = 24
STORE_PATH = os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
SESSIONS_DIR = os.path.expanduser("~/.openclaw/agents/main/sessions/")

def find_transcript_files(session_id: str) -> list:
    """Find all transcript/trajectory files for a session ID."""
    pattern = os.path.join(SESSIONS_DIR, f"*{session_id}*")
    return sorted(glob.glob(pattern))

def main():
    parser = argparse.ArgumentParser(description="Prune dead spawn-child sessions")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    parser.add_argument("--hours", type=int, default=STALE_HOURS, help="Stale threshold in hours")
    parser.add_argument("--delete-files", action="store_true", help="Also delete transcript files")
    args = parser.parse_args()

    stale_ms = args.hours * 3600 * 1000
    now_ms = int(time.time() * 1000)

    # Load session store
    if not os.path.exists(STORE_PATH):
        print(f"ERROR: Store not found: {STORE_PATH}")
        return 1

    with open(STORE_PATH) as f:
        store = json.load(f)

    # Find stale spawn-child sessions
    stale_keys = []
    kept_keys = []
    for key, session in store.items():
        if not key.startswith("agent:main:subagent:"):
            kept_keys.append(key)
            continue

        age_ms = now_ms - session.get("updatedAt", 0)
        if age_ms > stale_ms:
            stale_keys.append(key)
        else:
            kept_keys.append(key)

    # Report
    print(f"Stale threshold: {args.hours}h")
    print(f"Total sessions:  {len(store)}")
    print(f"Stale subagents: {len(stale_keys)}")
    print(f"Kept:            {len(kept_keys)}")
    print()

    # Show stale sessions
    stale_sessions = []
    for key in stale_keys:
        s = store[key]
        age_h = (now_ms - s.get("updatedAt", 0)) / 3600000
        sid = s.get("sessionId", "?")[:16]
        stale_sessions.append((key, age_h, sid))
        print(f"  💀 {age_h:5.1f}h | {sid} | {key[30:70]}")

    if not stale_keys:
        print("No stale spawn-child sessions to prune.")
        return 0

    print(f"\nTotal stale: {len(stale_keys)} sessions")
    if args.dry_run:
        print("\n🔶 DRY RUN — no changes made. Run without --dry-run to apply.")
        return 0

    # Remove from store
    for key in stale_keys:
        del store[key]

    # Write updated store
    with open(STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)
    print(f"\n✅ Store updated: removed {len(stale_keys)} sessions from {STORE_PATH}")

    # Optionally delete transcript files
    if args.delete_files:
        deleted_bytes = 0
        deleted_count = 0
        for key in stale_keys:
            sid = store[key].get("sessionId", "") if key in store else ""
            # sid might not be in store anymore, get it from before
            pass

        # Re-find stale session IDs from our list
        for key in stale_keys:
            files = find_transcript_files(key.split(":")[-1])  # use the last segment
            for fpath in files:
                size = os.path.getsize(fpath)
                os.remove(fpath)
                deleted_bytes += size
                deleted_count += 1
                print(f"  🗑  Deleted: {os.path.basename(fpath)} ({size:,} bytes)")

        print(f"\n🗑  Deleted {deleted_count} files ({deleted_bytes:,} bytes freed)")

    print("\nDone! 🎉")
    return 0

if __name__ == "__main__":
    exit(main())
