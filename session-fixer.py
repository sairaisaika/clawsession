#!/usr/bin/env python3
"""
session-fixer.py — Fix stale spawn-child sessions

For old spawn-child sessions (default >12h), reset token counters and
clean up bloated entries. This fixes the #98982 compaction deadlock
where totalTokens was never reset after compaction.

Safe operations:
  - Reset totalTokens to match actual transcript size
  - Remove sessions whose transcript files are missing
  - Never touches active direct/cron/main sessions

Usage:
  ./session-fixer.py --dry-run     # Preview only
  ./session-fixer.py               # Apply fixes
  ./session-fixer.py --aggressive  # Also remove stale subagents >24h
"""

import json
import os
import glob
import time
import sys
import subprocess
import argparse

STORE = os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
SESSIONS_DIR = os.path.expanduser("~/.openclaw/agents/main/sessions/")
STALE_MS = 12 * 3600 * 1000  # 12 hours
DELETE_MS = 24 * 3600 * 1000  # 24 hours (for aggressive)

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4

def count_transcript_tokens(session_id: str) -> tuple[int, int]:
    """Count actual tokens in the transcript file.
    Returns (total_input_tokens, total_output_tokens).
    """
    pattern = os.path.join(SESSIONS_DIR, f"*{session_id}*.jsonl")
    files = sorted(glob.glob(pattern))
    # Exclude .deleted. files and trajectory files
    files = [f for f in files if ".deleted." not in f and ".trajectory" not in f]

    if not files:
        return (0, 0)

    total_input = 0
    total_output = 0
    try:
        with open(files[0]) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                # Handle different transcript formats
                role = entry.get("role", "")
                content = entry.get("content", "")
                
                # Nested format: entry.message.role / entry.message.content
                if not role and "message" in entry:
                    role = entry["message"].get("role", "")
                    content = entry["message"].get("content", "")
                
                # Skip non-message entries
                if not role:
                    continue
                    
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += block.get("text", "")

                if role == "user" or role == "system":
                    total_input += estimate_tokens(text)
                elif role == "assistant":
                    total_output += estimate_tokens(text)
    except Exception as e:
        print(f"  ⚠️  Error reading transcript: {e}", file=sys.stderr)

    return (total_input, total_output)

def main():
    parser = argparse.ArgumentParser(description="Fix stale spawn-child session token counts")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--aggressive", action="store_true",
                        help="Also remove stale subagent entries >24h")
    parser.add_argument("--hours", type=int, default=12,
                        help="Stale threshold in hours (default: 12)")
    args = parser.parse_args()

    stale_ms = args.hours * 3600 * 1000
    delete_ms = DELETE_MS
    now_ms = int(time.time() * 1000)

    if not os.path.exists(STORE):
        print(f"ERROR: Store not found: {STORE}", file=sys.stderr)
        return 1

    with open(STORE) as f:
        store = json.load(f)

    fixed = 0
    removed = 0
    kept = 0
    total_tokens_freed = 0

    print(f"\n{'='*60}")
    print(f"  🔧 Session Fixer — {'DRY RUN' if args.dry_run else 'APPLYING'}")
    print(f"  Stale threshold: {args.hours}h")
    print(f"  Total sessions:  {len(store)}")
    print(f"{'='*60}\n")

    for key, session in list(store.items()):
        if not key.startswith("agent:main:subagent:"):
            kept += 1
            continue

        age_ms = now_ms - session.get("updatedAt", 0)
        age_h = age_ms / 3600000
        total_tokens = session.get("totalTokens", 0) or 0
        context_tokens = session.get("contextTokens", 1000000) or 1000000
        pct = total_tokens / context_tokens * 100 if context_tokens else 0
        session_id = session.get("sessionId", "")
        compacted = session.get("compactionCount", 0)

        # Skip recent sessions
        if age_ms < stale_ms and pct < 100:
            kept += 1
            continue

        # Determine action
        action = None
        reason = ""

        # Check if transcript exists
        actual_in, actual_out = count_transcript_tokens(session_id)
        actual_total = actual_in + actual_out

        # Case 1: No transcript → remove
        if actual_total == 0 and age_ms > delete_ms:
            action = "remove"
            reason = f"no transcript ({age_h:.0f}h old)"

        # Case 2: Tokens wildly inflated → reset
        elif total_tokens > actual_total * 3 and actual_total > 0:
            action = "reset"
            saved = total_tokens - actual_total
            reason = f"token reset {total_tokens:,}→{actual_total:,} (saved {saved:,})"
            total_tokens_freed += saved

        # Case 3: Tokens over 100% but already compacted → reset
        elif pct > 100 and compacted > 0:
            if actual_total > 0:
                action = "reset"
                saved = total_tokens - actual_total
                reason = f"post-compact reset {total_tokens:,}→{actual_total:,} (saved {saved:,})"
                total_tokens_freed += saved
            elif age_ms > delete_ms:
                action = "remove"
                reason = f"over 100% but no transcript ({age_h:.0f}h)"

        # Case 4: Aggressive → old subagents removed
        elif args.aggressive and age_ms > delete_ms:
            action = "remove"
            reason = f"aggressive prune ({age_h:.0f}h old)"

        if action:
            marker = "🟢" if action == "reset" else "🔴"
            print(f"  {marker} [{action:6s}] {reason}")
            print(f"     Key: {key[30:80]}")

            if action == "remove":
                removed += 1
            elif action == "reset":
                fixed += 1

            if not args.dry_run:
                if action == "remove":
                    # Also delete transcript files
                    pattern = os.path.join(SESSIONS_DIR, f"*{session_id}*")
                    for fp in glob.glob(pattern):
                        try:
                            os.remove(fp)
                        except OSError:
                            pass
                    del store[key]
                elif action == "reset":
                    session["totalTokens"] = actual_total
                    if actual_in > 0:
                        session["inputTokens"] = actual_in
                    if actual_out > 0:
                        session["outputTokens"] = actual_out
                    session["lastFixedAt"] = int(time.time() * 1000)
                    session["fixedBy"] = "session-fixer"
        else:
            kept += 1

    # Write updated store
    if not args.dry_run and (fixed > 0 or removed > 0):
        with open(STORE, "w") as f:
            json.dump(store, f, indent=2)
        print(f"\n{'='*60}")
        print(f"  ✅ Store saved: {STORE}")
        print(f"  Fixed: {fixed}  Removed: {removed}  Kept: {kept}")
        if total_tokens_freed:
            print(f"  Tokens freed: {total_tokens_freed:,}")
        print(f"{'='*60}")

        # Run cleanup to remove unreferenced artifacts
        print("\n  Running session cleanup...")
        subprocess.run(["openclaw", "sessions", "cleanup", "--enforce", "--all-agents"],
                       capture_output=True, timeout=30)
        print("  Cleanup complete.")
    elif args.dry_run:
        print(f"\n{'='*60}")
        print(f"  🔶 DRY RUN — would fix {fixed}, remove {removed}, keep {kept}")
        print(f"  Tokens would free: {total_tokens_freed:,}")
        print(f"{'='*60}")

    return 0

if __name__ == "__main__":
    exit(main())
