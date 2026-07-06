#!/usr/bin/env python3
"""
clawsession-mcp.py — OpenClaw session lifecycle MCP server

Exposes session health monitoring, compaction, and AM archiving
as MCP tools. Runs over stdio transport.

Tools:
  - session_health      → Check all sessions, return problems
  - session_compact     → Compact a session + archive to AM
  - session_prune_stale → Remove dead subagents + archive to AM
  - session_fix_tokens  → Reset inflated token counters
"""

import json
import sys
import subprocess
import os
import time
import glob
import traceback

# ─── Config ─────────────────────────────────────────────────────────────
STORE = os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
SESSIONS_DIR = os.path.expanduser("~/.openclaw/agents/main/sessions/")
AM_URL = "http://localhost:3111/agentmemory/remember"
WARN_PCT = 80
CRIT_PCT = 90
DEFAULT_MAX_TOKENS = 1000000

# ─── MCP Protocol ───────────────────────────────────────────────────────

def mcp_send(msg: dict):
    """Send a JSON-RPC message to stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def mcp_log(msg: str):
    """Send log message via MCP notification."""
    mcp_send({
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {"level": "info", "data": msg}
    })

def mcp_result(id, result):
    mcp_send({"jsonrpc": "2.0", "id": id, "result": result})

def mcp_error(id, code, message):
    mcp_send({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})

# ─── AM Integration ─────────────────────────────────────────────────────

def am_save(content: str, project: str = "clawsession",
            mem_type: str = "archive", concepts: list = None):
    """Save a record to AgentMemory via REST API."""
    try:
        body = {
            "content": content,
            "project": project,
            "type": mem_type,
        }
        if concepts:
            body["concepts"] = concepts
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", AM_URL,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(body)],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            resp = json.loads(result.stdout)
            return resp.get("memory", {}).get("id", "ok")
        return None
    except Exception as e:
        mcp_log(f"AM save failed: {e}")
        return None

def am_lesson(content: str, project: str = "clawsession",
              confidence: float = 0.7, tags: list = None):
    """Save a lesson to AgentMemory."""
    try:
        body = {
            "content": content,
            "project": project,
            "confidence": confidence,
        }
        if tags:
            body["tags"] = tags
        # AM uses a different endpoint for lessons
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "http://localhost:3111/agentmemory/learn",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(body)],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            resp = json.loads(result.stdout)
            return resp.get("lesson", {}).get("id", "ok")
        return None
    except Exception as e:
        mcp_log(f"AM lesson failed: {e}")
        return None

# ─── Session Utilities ─────────────────────────────────────────────────

def get_store():
    """Load the session store JSON."""
    if not os.path.exists(STORE):
        return {}
    with open(STORE) as f:
        return json.load(f)

def save_store(store):
    """Write the session store JSON."""
    with open(STORE, "w") as f:
        json.dump(store, f, indent=2)

def count_transcript_tokens(session_id: str) -> tuple:
    """Count actual tokens in the transcript file."""
    pattern = os.path.join(SESSIONS_DIR, f"*{session_id}*.jsonl")
    files = sorted(glob.glob(pattern))
    files = [f for f in files if ".deleted." not in f and ".trajectory" not in f]
    if not files:
        return (0, 0)

    total_in = total_out = 0
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
                role = entry.get("role", "")
                content = entry.get("content", "")
                if not role and "message" in entry:
                    role = entry["message"].get("role", "")
                    content = entry["message"].get("content", "")
                if not role:
                    continue
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += block.get("text", "")
                tokens = len(text) // 4
                if role in ("user", "system"):
                    total_in += tokens
                elif role == "assistant":
                    total_out += tokens
    except Exception:
        pass
    return (total_in, total_out)

def truncate_key(key: str, max_len: int = 50) -> str:
    short = key.replace("agent:", "").replace("telegram:direct:", "tg:")
    if len(short) > max_len:
        short = "..." + short[-(max_len - 3):]
    return short

# ─── Tool Implementations ───────────────────────────────────────────────

def tool_health() -> dict:
    """Check all sessions and return problems."""
    store = get_store()
    alerts = []
    for key, session in store.items():
        if not isinstance(session, dict):
            continue
        total_tokens = session.get("totalTokens", 0) or 0
        max_tokens = session.get("contextTokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS
        pct = (total_tokens / max_tokens * 100) if max_tokens > 0 else 0
        aborted = session.get("abortedLastRun", False)
        age_ms = int(time.time() * 1000) - (session.get("updatedAt", 0) or 0)
        age_h = round(age_ms / 3600000, 1)

        short = truncate_key(key)
        if pct >= CRIT_PCT:
            alerts.append({"level": "critical", "key": key, "pct": pct,
                          "tokens": total_tokens, "ageHours": age_h})
        elif pct >= WARN_PCT:
            alerts.append({"level": "warning", "key": key, "pct": pct,
                          "tokens": total_tokens, "ageHours": age_h})
        elif aborted:
            alerts.append({"level": "aborted", "key": key, "ageHours": age_h})

    all_tokens = [s.get("totalTokens", 0) or 0 for s in store.values() if isinstance(s, dict)]
    summary = {
        "total_keys": len(store),
        "total_tokens": sum(all_tokens),
        "max_token_pct": max(
            (s.get("totalTokens", 0) or 0) / ((s.get("contextTokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS) or 1) * 100
            for s in store.values() if isinstance(s, dict)
        ) if store else 0,
        "alerts": len([a for a in alerts if a["level"] == "critical"]),
        "warnings": len([a for a in alerts if a["level"] == "warning"]),
    }
    return {"summary": summary, "alerts": alerts}

def tool_compact(key: str, max_lines: int = 200) -> dict:
    """Compact a session and save to AM before doing so."""
    store = get_store()
    session = store.get(key)
    if not session:
        return {"error": f"session not found: {key}"}

    # Step 1: Archive to AM
    sid = session.get("sessionId", "")[:16]
    tt = session.get("totalTokens", 0) or 0
    age_h = round((int(time.time() * 1000) - (session.get("updatedAt", 0) or 0)) / 3600000, 1)
    topic = session.get("spawnedBy", "") or key.split(":")[-1][:16]

    am_id = am_save(
        content=(
            f"MCP session_compact: pre-compaction archive\n"
            f"  Key: {key}\n  Session: {sid}\n  Tokens: {tt:,}\n"
            f"  Age: {age_h}h\n  Topic: {topic}"
        ),
        mem_type="archive",
        concepts=["session-compact", "mcp", f"sid-{sid}"]
    )

    # Step 2: Try CLI compaction
    agent = session.get("agentId", "main")
    result = subprocess.run(
        ["openclaw", "sessions", "compact", key,
         "--agent", agent, "--max-lines", str(max_lines),
         "--timeout", "180000"],
        capture_output=True, text=True, timeout=200
    )

    if result.returncode == 0:
        # Step 3: Save lesson about this session
        am_lesson(
            content=f"Session compacted via MCP: {key} ({sid}, {tt:,}t, {age_h}h old). Topic: {topic}",
            tags=["compaction", "session-lifecycle", topic]
        )
        return {
            "success": True,
            "am_archive_id": am_id,
            "output": result.stdout[:500]
        }
    else:
        return {
            "success": False,
            "error": result.stderr[:300],
            "am_archive_id": am_id
        }

def tool_fix_tokens(hours: int = 12, dry_run: bool = True) -> dict:
    """Reset inflated token counters for stale subagent sessions."""
    store = get_store()
    stale_ms = hours * 3600 * 1000
    now_ms = int(time.time() * 1000)

    fixed = removed = kept = 0
    results = []

    for key, session in list(store.items()):
        if not isinstance(session, dict):
            continue
        if not key.startswith("agent:main:subagent:"):
            kept += 1
            continue

        age_ms = now_ms - (session.get("updatedAt", 0) or 0)
        total_tokens = session.get("totalTokens", 0) or 0
        max_tokens = session.get("contextTokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS
        pct = total_tokens / max_tokens * 100 if max_tokens else 0
        session_id = session.get("sessionId", "")
        compacted = session.get("compactionCount", 0)

        if age_ms < stale_ms and pct < 100:
            kept += 1
            continue

        actual_in, actual_out = count_transcript_tokens(session_id)
        actual = actual_in + actual_out

        if actual == 0 and age_ms > stale_ms * 2:
            # Remove dead session
            if not dry_run:
                # Archive first
                am_save(
                    content=f"MCP session_prune: removed dead subagent {key}\nSession: {session_id}\nAge: {age_ms/3600000:.0f}h",
                    mem_type="archive",
                    concepts=["session-prune", "mcp", f"sid-{session_id}"]
                )
                # Delete transcript files
                pattern = os.path.join(SESSIONS_DIR, f"*{session_id}*")
                for fp in glob.glob(pattern):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
                del store[key]
            removed += 1
            results.append({"key": key, "action": "remove", "ageH": round(age_ms/3600000, 1)})

        elif total_tokens > actual * 3 and actual > 0:
            # Reset inflated tokens
            if not dry_run:
                saved = total_tokens - actual
                session["totalTokens"] = actual
                if actual_in > 0:
                    session["inputTokens"] = actual_in
                if actual_out > 0:
                    session["outputTokens"] = actual_out
                session["lastFixedAt"] = int(time.time() * 1000)
                session["fixedBy"] = "clawsession-mcp"
                # Archive
                am_save(
                    content=f"MCP token_reset: {key} ({total_tokens:,}→{actual:,}, saved {saved:,})",
                    mem_type="archive",
                    concepts=["token-reset", "mcp", f"sid-{session_id}"]
                )
            fixed += 1
            results.append({"key": key, "action": "reset",
                           "before": total_tokens, "after": actual})

    if not dry_run:
        save_store(store)

    return {
        "dry_run": dry_run,
        "fixed": fixed,
        "removed": removed,
        "kept": kept,
        "results": results[:20]  # First 20 only
    }

def tool_prune_stale(hours: int = 24, dry_run: bool = True) -> dict:
    """Remove stale dead subagent sessions with AM archive."""
    return tool_fix_tokens(hours=hours, dry_run=dry_run)

# ─── Tool Registry ──────────────────────────────────────────────────────

TOOLS = {
    "session_health": {
        "description": "Check all OpenClaw sessions for health problems: token usage >80% (warning) or >90% (critical), aborted runs.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda params: tool_health()
    },
    "session_compact": {
        "description": "Compact a session by key, saving its archive to AgentMemory first. Requires the full session key from session_health output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Full session key (e.g. agent:main:subagent:xxx)"},
                "max_lines": {"type": "integer", "description": "Max transcript lines to keep", "default": 200}
            },
            "required": ["key"]
        },
        "handler": lambda params: tool_compact(params["key"], params.get("max_lines", 200))
    },
    "session_fix_tokens": {
        "description": "Reset inflated token counters for stale subagent sessions. Run with dry_run=true first to preview.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Stale threshold in hours", "default": 12},
                "dry_run": {"type": "boolean", "description": "Preview only, no changes", "default": True}
            },
            "required": []
        },
        "handler": lambda params: tool_fix_tokens(
            params.get("hours", 12), params.get("dry_run", True)
        )
    },
    "session_prune_stale": {
        "description": "Remove dead subagent sessions older than N hours, with AM archive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Age threshold in hours", "default": 24},
                "dry_run": {"type": "boolean", "description": "Preview only", "default": True}
            },
            "required": []
        },
        "handler": lambda params: tool_prune_stale(
            params.get("hours", 24), params.get("dry_run", True)
        )
    },
    "session_list_active": {
        "description": "List active sessions with token usage, sorted by usage descending.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}, "required": []},
        "handler": lambda params: _list_active(params.get("limit", 20))
    }
}

def _list_active(limit=20):
    store = get_store()
    sessions = []
    for key, s in store.items():
        if not isinstance(s, dict):
            continue
        tt = s.get("totalTokens", 0) or 0
        ct = s.get("contextTokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS
        pct = round(tt / ct * 100, 1) if ct else 0
        sessions.append({
            "key": key[:80],
            "tokens": tt,
            "pct": pct,
            "kind": key.split(":")[2] if ":" in key else "?",
            "ageH": round((int(time.time() * 1000) - (s.get("updatedAt", 0) or 0)) / 3600000, 1),
            "aborted": s.get("abortedLastRun", False)
        })
    sessions.sort(key=lambda x: x["pct"], reverse=True)
    return {"sessions": sessions[:limit], "total": len(sessions)}

# ─── MCP Server Loop ────────────────────────────────────────────────────

def handle_initialize(msg_id):
    """Respond to MCP initialize request."""
    mcp_result(msg_id, {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {
            "name": "clawsession",
            "version": "0.1.0"
        }
    })

def main():
    """MCP server main loop over stdio."""
    initialized = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "initialize":
            handle_initialize(msg_id)
            initialized = True
            continue

        if not initialized:
            continue

        if method == "ping":
            mcp_result(msg_id, {})
            continue

        if method == "tools/list":
            tools_list = [
                {
                    "name": name,
                    "description": info["description"],
                    "inputSchema": info.get("inputSchema", {"type": "object", "properties": {}})
                }
                for name, info in TOOLS.items()
            ]
            mcp_result(msg_id, {"tools": tools_list})
            continue

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})

            if tool_name not in TOOLS:
                mcp_error(msg_id, -32601, f"Tool not found: {tool_name}")
                continue

            try:
                result = TOOLS[tool_name]["handler"](tool_args)
                mcp_result(msg_id, {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]})
            except Exception as e:
                mcp_error(msg_id, -32603, f"Tool error: {e}\n{traceback.format_exc()}")
            continue

        if method == "notifications/initialized":
            continue

        # Unknown method
        mcp_error(msg_id, -32601, f"Method not found: {method}")

if __name__ == "__main__":
    main()
