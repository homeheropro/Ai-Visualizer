# ai-visualizer: give your AI agent a face.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads Claude Code's own on-disk session records — no new data store,
just a window onto files Claude Code already writes.

Two sources, and they answer different questions:

  ~/.claude/sessions/<pid>.json    ONE PER RUNNING PROCESS, right now.
                                    Has sessionId, cwd, name, status,
                                    statusUpdatedAt. This is the only
                                    place "status" exists at all.

  ~/.claude/projects/<proj>/*.jsonl  ONE PER SESSION EVER RUN, finished
                                    or not. JSONL, one message per line.
                                    No status field — a finished session
                                    and a crashed one look identical
                                    here, just a file that stopped
                                    growing.

CONFIRMED FROM ONE LIVE EXAMPLE, NOT DOCUMENTED ANYWHERE: a sessions/
file looks like {"pid":..., "sessionId":..., "cwd":..., "status":"busy",
"statusUpdatedAt":..., "name":..., "peerFeatures":["notify_idle",...]}.
Only "busy" has actually been observed. Whether "idle" or some other
value is what a session waiting on its user looks like is NOT verified
here — this module reports the raw status string rather than guessing
a "waiting on you" boolean from it. Confirm the other values before any
dashboard tile asserts a session is waiting rather than just not-busy.

The `notify_idle` peer feature suggests Claude Code may already have an
idle-notification mechanism built in. Not investigated here — a lead
for a later session, not something this module leans on.
"""
import json
import os
from pathlib import Path

CLAUDE_DIR = Path(os.path.expanduser("~")) / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None


def live_sessions() -> dict[str, dict]:
    """{sessionId: {pid, cwd, name, status, statusUpdatedAt, startedAt}}
    for every Claude Code process running on this machine right now.
    Empty dict if the folder doesn't exist or holds nothing readable."""
    out = {}
    if not SESSIONS_DIR.is_dir():
        return out
    for p in SESSIONS_DIR.glob("*.json"):
        rec = _read_json(p)
        if not rec or not rec.get("sessionId"):
            continue
        out[rec["sessionId"]] = rec
    return out


def _find_cwd(path: Path, max_lines: int = 20) -> str:
    """Not every line carries cwd (mode/file-history-snapshot lines at
    the top of a session often don't), so this reads forward past a
    handful of leading lines until one does, rather than trusting the
    very first line blindly. Gives up after max_lines — a session with
    no cwd on any early line reports empty rather than reading further
    and further into a file just to fill one label."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cwd = obj.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        pass
    return ""


def _tail_activity(path: Path, chunk_bytes: int = 8192) -> tuple[str, str]:
    """(last_role, last_timestamp) without reading the file whole — seeks
    back chunk_bytes from the end (or the whole file, if smaller). The
    JSONL's last line is often a bookkeeping line (last-prompt,
    cost-state, atis-latch — confirmed present in real files here, not
    just theoretical), not a conversational turn, so this walks backward
    from the end and reports the last line that actually carries a
    message role; falls back to the very last line's own `type` if
    nothing in the chunk has one. A line longer than chunk_bytes (a huge
    single message) falls back to empty rather than growing the read —
    acceptable for a dashboard glance, not guaranteed exhaustive."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > chunk_bytes:
                f.seek(-chunk_bytes, os.SEEK_END)
            chunk = f.read()
        lines = [l.strip() for l in chunk.decode("utf-8", "ignore")
                 .splitlines() if l.strip()]
        fallback_type, fallback_ts = "", ""
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not fallback_type:
                fallback_type = obj.get("type") or ""
                fallback_ts = obj.get("timestamp") or ""
            role = (obj.get("message") or {}).get("role")
            if role:
                return role, obj.get("timestamp") or ""
        return fallback_type, fallback_ts
    except OSError:
        return "", ""


def list_sessions() -> list[dict]:
    """One row per session file ever written, across every project,
    newest first. Merges in live status where the session is currently
    running. Best-effort throughout: an unreadable file is skipped, not
    a crash."""
    live = live_sessions()
    matched = set()
    out = []
    if PROJECTS_DIR.is_dir():
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            for sess_file in proj_dir.glob("*.jsonl"):
                sid = sess_file.stem
                cwd = _find_cwd(sess_file)
                last_role, last_ts = _tail_activity(sess_file)
                try:
                    mtime = sess_file.stat().st_mtime
                except OSError:
                    mtime = 0
                live_rec = live.get(sid)
                if live_rec:
                    matched.add(sid)
                out.append(_row(sid, proj_dir.name, cwd, last_role, last_ts,
                                 mtime, live_rec))
    # A session can go live before its transcript file exists (or between
    # a project folder listing and this loop finishing) -- without this,
    # a session that is genuinely running right now would be silently
    # absent rather than merely lacking history, which is the one failure
    # this module exists to avoid.
    for sid, live_rec in live.items():
        if sid in matched:
            continue
        ts = (live_rec.get("statusUpdatedAt") or live_rec.get("startedAt")
              or 0) / 1000
        out.append(_row(sid, "(no transcript yet)", live_rec.get("cwd", ""),
                         "", "", ts, live_rec))
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _row(sid, project_folder, cwd, last_role, last_ts, mtime, live_rec):
    return {
        "session_id": sid,
        "project_folder": project_folder,
        "cwd": cwd or (live_rec or {}).get("cwd") or "",
        "last_role": last_role,
        "last_ts": last_ts,
        "mtime": mtime,
        "live": live_rec is not None,
        "status": live_rec.get("status") if live_rec else None,
        "name": live_rec.get("name") if live_rec else None,
        "kind": live_rec.get("kind") if live_rec else None,
        "entrypoint": live_rec.get("entrypoint") if live_rec else None,
        "started_at": live_rec.get("startedAt") if live_rec else None,
    }


def _transcript_path(session_id: str) -> Path | None:
    """Finds a session's own .jsonl by walking PROJECTS_DIR and matching
    the filename stem — never by building a path out of the id string
    directly, so a crafted session id can't walk anywhere it shouldn't."""
    if not PROJECTS_DIR.is_dir():
        return None
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            parts.append(block.get("text", ""))
        elif t == "tool_use":
            parts.append(f"[tool call: {block.get('name', '?')}]")
        elif t == "tool_result":
            parts.append("[tool result]")
    return "\n".join(p for p in parts if p)


def recent_messages(session_id: str, limit: int = 20,
                     tail_bytes: int = 200_000) -> list[dict]:
    """The last `limit` user/assistant turns from a session's own
    transcript, oldest first. Reads only the tail of the file, same
    trade as _tail_activity: a line split by the seek point is a
    parse failure that gets skipped, not a reason to read the whole
    file. Best-effort throughout -- a missing or unreadable file
    returns an empty list rather than raising."""
    path = _transcript_path(session_id)
    if path is None:
        return []
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(-tail_bytes, os.SEEK_END)
            chunk = f.read()
    except OSError:
        return []
    out = []
    for line in chunk.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(msg.get("content"))
        if not text:
            continue
        out.append({"role": role, "text": text,
                     "ts": obj.get("timestamp", "")})
    return out[-limit:]


def list_projects() -> list[dict]:
    """The same session rows, rolled up by project folder — no separate
    data source, just a different grouping of list_sessions()."""
    by_project: dict[str, dict] = {}
    for row in list_sessions():
        p = by_project.setdefault(row["project_folder"], {
            "project_folder": row["project_folder"],
            "cwd": row["cwd"],
            "session_count": 0,
            "live_count": 0,
            "last_mtime": 0,
        })
        p["session_count"] += 1
        if row["live"]:
            p["live_count"] += 1
        p["last_mtime"] = max(p["last_mtime"], row["mtime"])
        if not p["cwd"]:
            p["cwd"] = row["cwd"]
    return sorted(by_project.values(),
                  key=lambda r: r["last_mtime"], reverse=True)
