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
    out = []
    if not PROJECTS_DIR.is_dir():
        return out
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
            row = {
                "session_id": sid,
                "project_folder": proj_dir.name,
                "cwd": cwd or (live_rec or {}).get("cwd") or "",
                "last_role": last_role,
                "last_ts": last_ts,
                "mtime": mtime,
                "live": live_rec is not None,
                "status": live_rec.get("status") if live_rec else None,
                "name": live_rec.get("name") if live_rec else None,
            }
            out.append(row)
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


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
