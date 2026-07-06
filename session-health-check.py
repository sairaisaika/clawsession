#!/usr/bin/env python3
"""
session-health-check.py — OpenClaw Session Health Watcher

Monitors all active OpenClaw sessions for:
  - Token usage nearing limits (≥80% warn, ≥90% crit)
  - Aborted last runs
  - Stale spawn-child sessions
  - Total token budget consumption across agents

Usage:
  ./session-health-check.py              # Human-readable report
  ./session-health-check.py --json       # JSON report (for cron or tooling)
  ./session-health-check.py --alert-only # Problems only
  ./session-health-check.py --compact    # Auto-compact sessions >80%
"""

import json
import subprocess
import sys
import time
import os

# ─── Config ─────────────────────────────────────────────────────────────
WARN_PCT = 80
CRIT_PCT = 90
STALE_MS = 24 * 3600 * 1000   # 24 hours
COMPACT_THRESHOLD_PCT = 80
COMPACT_COOLDOWN_MS = 5 * 60 * 1000  # 5 min — don't compact very recent
COMPACT_MAX_LINES = 200
COMPACT_TIMEOUT_SEC = 130

DEFAULT_MAX_TOKENS = 1000000

# ─── Helpers ────────────────────────────────────────────────────────────

def get_sessions():
    """Fetch all sessions via CLI."""
    result = subprocess.run(
        ["openclaw", "sessions", "list", "--all-agents", "--json"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"ERROR: openclaw CLI failed: {result.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(result.stdout)
    return data.get("sessions", []), data.get("totalCount", 0)

def truncate_key(key: str, max_len: int = 50) -> str:
    """Shorten session key for display."""
    short = key.replace("agent:", "").replace("telegram:direct:", "tg:")
    if len(short) > max_len:
        short = "..." + short[-(max_len - 3):]
    return short

def compact_session(key: str, agent: str) -> dict:
    """Compact a single session via CLI."""
    result = subprocess.run(
        [
            "openclaw", "sessions", "compact", key,
            "--agent", agent,
            "--max-lines", str(COMPACT_MAX_LINES),
            "--timeout", str(COMPACT_TIMEOUT_SEC * 1000)
        ],
        capture_output=True, text=True, timeout=COMPACT_TIMEOUT_SEC + 10
    )
    return {
        "success": result.returncode == 0,
        "key": key,
        "error": result.stderr[:300] if result.returncode != 0 else None
    }

def analyze(sessions):
    """Analyze sessions and return report dict."""
    now_ms = int(time.time() * 1000)
    report = {
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        "totalSessions": 0,
        "activeSessions": len(sessions),
        "alerts": [],
        "critical": [],
        "warnings": [],
        "healthy": [],
        "summary": {}
    }

    for s in sessions:
        key = s.get("key", "?")
        kind = s.get("kind", "?")
        agent = s.get("agentId", "?")
        total_tokens = s.get("totalTokens", 0) or 0
        max_tokens = s.get("contextTokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS
        pct = (total_tokens / max_tokens * 100) if max_tokens > 0 else 0
        age_ms = s.get("ageMs", 0) or 0
        aborted = s.get("abortedLastRun", False)
        model = s.get("model", "?")
        short_key = truncate_key(key)

        entry = {
            "key": key, "short": short_key,
            "kind": kind, "agent": agent,
            "tokens": total_tokens, "maxTokens": max_tokens,
            "pct": round(pct, 1),
            "ageMs": age_ms, "ageHours": round(age_ms / 3600000, 1),
            "aborted": aborted, "model": model
        }

        if pct >= CRIT_PCT:
            entry["level"] = "critical"
            report["critical"].append(entry)
            report["alerts"].append(
                f"🔴 CRITICAL: {short_key} — {pct:.0f}% tokens ({total_tokens:,}/{max_tokens:,})"
            )
        elif pct >= WARN_PCT:
            entry["level"] = "warning"
            report["warnings"].append(entry)
            report["alerts"].append(
                f"🟡 WARNING: {short_key} — {pct:.0f}% tokens ({total_tokens:,}/{max_tokens:,})"
            )
        elif aborted:
            entry["level"] = "aborted"
            report["warnings"].append(entry)
            report["alerts"].append(
                f"⚠️ ABORTED: {short_key} — last run aborted"
            )
        elif age_ms > STALE_MS and kind == "spawn-child":
            entry["level"] = "stale"
            report["warnings"].append(entry)
        else:
            entry["level"] = "ok"
            report["healthy"].append(entry)

    all_tokens = [s.get("totalTokens", 0) or 0 for s in sessions]
    report["summary"] = {
        "totalTokens": sum(all_tokens),
        "maxTokens": max(all_tokens) if all_tokens else 0,
        "avgTokens": round(sum(all_tokens) / len(all_tokens), 0) if all_tokens else 0,
        "criticalCount": len(report["critical"]),
        "warningCount": len(report["warnings"]),
        "healthyCount": len(report["healthy"]),
        "alertCount": len(report["alerts"])
    }

    return report

def print_human_report(report):
    """Print a nicely formatted human-readable report."""
    s = report["summary"]

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║      🩺  OpenClaw Session Health Report                 ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print(f"  Sessions: {report['activeSessions']} active / {report['totalSessions']} total")
    print(f"  Tokens:   {s['totalTokens']:>8,} total  ·  {s['avgTokens']:>6,.0f} avg  ·  {s['maxTokens']:>6,} max")
    print(f"  Status:   🟢 {s['healthyCount']} healthy  🟡 {s['warningCount']} warnings  🔴 {s['criticalCount']} critical")
    print()

    if s["alertCount"] > 0:
        print("  ── Alerts ──")
        for a in report["alerts"]:
            print(f"    {a}")
        print()

    if report["warnings"]:
        print("  ── Issues ──")
        for w in sorted(report["warnings"], key=lambda x: x["pct"], reverse=True):
            icon = {"warning": "🟡", "aborted": "⚠️ ", "stale": "💤"}.get(w["level"], "⚪")
            print(f"    {icon} {w['short']} ({w['tokens']:,}t, {w['ageHours']}h)")
        print()

    # Top 5 by token usage
    all_entries = report["healthy"] + report["warnings"] + report["critical"]
    all_entries.sort(key=lambda x: x["pct"], reverse=True)
    print("  ── Top Sessions by Token Usage ──")
    for e in all_entries[:5]:
        icon = {"critical": "🔴", "warning": "🟡", "ok": "🟢", "stale": "💤", "aborted": "⚠️ "}.get(e["level"], "⚪")
        print(f"    {icon} {e['pct']:6.1f}%  {e['short']}")

    print()
    print(f"  Report generated: {report['timestamp']}")
    print()

def print_alert_only(report):
    """Print only problems."""
    s = report["summary"]
    if s["alertCount"] == 0:
        print("🟢 All sessions healthy.")
        return
    for a in report["alerts"]:
        print(a)
    print(f"\nTotal alerts: {s['alertCount']}")

def do_compact(sessions):
    """Compact sessions above threshold (with cooldown)."""
    now_ms = int(time.time() * 1000)
    results = []

    for s in sessions:
        total_tokens = s.get("totalTokens", 0) or 0
        max_tokens = s.get("contextTokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS
        pct = (total_tokens / max_tokens * 100) if max_tokens > 0 else 0
        age_ms = s.get("ageMs", 0) or 0
        key = s.get("key", "")
        agent = s.get("agentId", "main")

        if pct >= COMPACT_THRESHOLD_PCT and age_ms > COMPACT_COOLDOWN_MS:
            short_key = truncate_key(key)
            print(f"  📦 Compacting: {short_key} ({pct:.1f}%)...", flush=True)
            result = compact_session(key, agent)
            if result["success"]:
                print(f"     ✅ Done")
                results.append(result)
            else:
                print(f"     ❌ {result['error']}")

    print(f"\nCompacted {len(results)} sessions.")
    return results

# ─── Main ───────────────────────────────────────────────────────────────

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--human"

    try:
        sessions, total_count = get_sessions()
    except Exception as e:
        print(f"ERROR: Could not fetch sessions: {e}", file=sys.stderr)
        sys.exit(1)

    report = analyze(sessions)
    report["totalSessions"] = total_count

    if mode == "--json":
        print(json.dumps(report, indent=2))
    elif mode == "--alert-only":
        print_alert_only(report)
    elif mode == "--compact":
        print_human_report(report)
        print("── Auto-Compact ──")
        do_compact(sessions)
    else:
        print_human_report(report)

    # Exit with code = number of criticals (for cron alerting)
    if mode != "--compact":
        sys.exit(min(report["summary"]["criticalCount"], 127))

if __name__ == "__main__":
    main()
