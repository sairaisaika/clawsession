# clawsession — OpenClaw Session Health & Lifecycle Management

External session-health watcher for OpenClaw, built as cron+scripts.
Tracks token usage, detects compaction deadlocks, and proactively manages session lifecycle.

## Motivation

OpenClaw issue [#97924](https://github.com/openclaw/openclaw/issues/97924) — "Session rotation/archive lifecycle event for cron job triggers" — has been open since 2026-06-29 with no implementation.
Related issues: #83338, #98982, #80674, #95443.

Rather than wait, this project implements external monitoring.

## Scripts

### `session-health-check.py`

Monitors all OpenClaw sessions for:
- **🟡 Token usage ≥80%** — approaching limit
- **🔴 Token usage ≥90%** — critical, needs compaction
- **⚠️ Aborted last run** — session may be corrupt
- **💤 Stale spawn-children** — unused >24h
- **Total token budget** — per-agent consumption

```
Usage:
  ./session-health-check.py                # Human-readable report
  ./session-health-check.py --json         # JSON output (for cron/tooling)
  ./session-health-check.py --alert-only   # Problems only
  ./session-health-check.py --compact      # Auto-compact sessions >80%
```

## Cron Setup (OpenClaw internal)

```json
{
  "name": "session-health-check",
  "schedule": { "kind": "every", "everyMs": 21600000 },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "Run: `~/.openclaw/workspace/scripts/session-health-check.py --alert-only`"
  }
}
```

## Roadmap

- [x] Session health check script
- [x] Cron job (every 6h)
- [ ] Auto-compaction (needs gateway pairing approval)
- [ ] Preemptive session rotation
- [ ] Webhook alerts for critical state
- [ ] Session budget trending / history

## Related Issues

| Issue | Status | Title |
|-------|--------|-------|
| [#97924](https://github.com/openclaw/openclaw/issues/97924) | P2, open | Session rotation/archive lifecycle event for cron job triggers |
| [#98982](https://github.com/openclaw/openclaw/issues/98982) | P1, open | Compaction dead-end with overflow blocks |
| [#83338](https://github.com/openclaw/openclaw/issues/83338) | P2, open | sessions_history blind to rotated transcripts |
| [#95443](https://github.com/openclaw/openclaw/issues/95443) | P1, open | 主 Telegram session 被 lifecycleGeneration 静默重置 |
