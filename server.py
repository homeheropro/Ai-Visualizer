#!/usr/bin/env python3
# ai-visualizer: give your AI agent a face.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ai-visualizer server. Python standard library only, nothing to install.

Serves the face gallery at http://127.0.0.1:8790/ and exposes:

  /state   polled by the faces (~8x/sec):
           {"state":  "idle|listening|thinking|speaking",
            "level":  0.0-1.0,       voice loudness while speaking
            "samples": [64 floats],  raw waveform snapshot (0s when quiet)
            "alert":  bool,          optional attention signal
            "loading": bool}         true while the voice line plays its
                                     own thinking sound (we stay quiet)
  /config  the merged ai-visualizer.json plus the list of installed
           faces, discovered by scanning the faces/ folder. Drop a new
           folder with an index.html into faces/ and it appears in the
           gallery. That is the whole plugin system.
  /sessions every Claude Code session this machine has a record of,
           newest first, live ones flagged — reads sessions.py, which
           reads Claude Code's own on-disk logs. Not part of the voice
           bus; works whether or not a voice line is running.
  /projects the same data rolled up by project folder.
  /plans   Plan Mode's own output at ~/.claude/plans, newest first. A
           viewer, not an approval tracker — see list_plans()'s
           docstring for what it can't tell you.
  /plan?file=<name>  one plan's raw text. <name> must be a bare
           filename (no slash, no backslash, no ".."), so this can
           never read outside the plans folder no matter what is
           passed.
  /transcript?session=<id>  the last 20 user/assistant turns of one
           session, oldest first. <id> is only ever used to match a
           filename ai-visualizer already discovered by walking
           PROJECTS_DIR itself (see sessions.py's _transcript_path) --
           never concatenated into a path, so this can't be pointed
           outside that folder either.

READ-ONLY on the signal bus. The bus is tiny files written by a voice
line (backtalk writes them natively, github.com/jaredrhod/backtalk):

  .voice_state         idle | listening | thinking | speaking
  .voice_waveform      JSON {ts, samples: [64 floats]} while audio plays
  .voice_loading_pid   exists while the voice line plays a thinking sound
  .voice_alert         optional: non-empty file = attention needed
  .voice_activity      JSON {ts, text} — what tool is running right now
  .voice_rate_limits   JSON usage by window — only if show_usage is on
  .voice_session_usage JSON {turns, in_tokens, out_tokens, cost} — only
                       if show_usage is on

Where the bus lives comes from "bus_dir" in ai-visualizer.json (default:
this folder). Point it at your backtalk folder, or point backtalk's
"signals_dir" here. Either direction works.

Run:
  python3 server.py             the real bus
  python3 server.py --mock speaking
                                no voice line needed: /state synthesizes
                                the chosen state (idle|listening|thinking
                                |speaking) so you can see a face perform
  python3 server.py --no-open   do not auto-open the browser
Ctrl-C stops.
"""
import json
import math
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
import urllib.request
import urllib.parse
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sessions as claude_sessions

HERE = Path(__file__).resolve().parent
STATES = {"idle", "listening", "thinking", "speaking"}
WAVEFORM_STALE_S = 0.6

DEFAULTS = {
    "name": "JARVIS",       # shown on the chip / headers, yours to change
    "badge": "",            # optional handle shown in some faces' chrome
    "face": "board",        # the default face the root URL opens
    "port": 8790,
    "bus_dir": "",          # where the .voice_* files live ("" = here)
    "thinking_sound": True, # play assets/thinking.wav while thinking
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        user = json.loads((HERE / "ai-visualizer.json").read_text())
        for k, v in user.items():
            cfg[k] = v
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"[config] ai-visualizer.json is not valid JSON ({e}), "
              f"using defaults")
    return cfg


CFG = load_config()
BUS = Path(CFG["bus_dir"]).expanduser() if CFG.get("bus_dir") else HERE

MOCK = None
NO_OPEN = "--no-open" in sys.argv
if "--mock" in sys.argv:
    i = sys.argv.index("--mock")
    MOCK = sys.argv[i + 1] if len(sys.argv) > i + 1 else "speaking"
    if MOCK not in STATES:
        MOCK = "speaking"
PORT = int(CFG.get("port", 8790))
if "--port" in sys.argv:
    i = sys.argv.index("--port")
    PORT = int(sys.argv[i + 1])

# BIND_HOST, added 2026-09-17 for phone access. Was 127.0.0.1 (loopback only);
# now every interface, so a phone on Marcus's Tailscale tailnet can load the
# dashboard directly. What keeps this private is NOT anything in this file -
# there is no token or Origin check here, unlike athena_desk.py - it is the
# Windows Firewall rule Set-AthenaRemoteAccess.ps1 scopes to 100.64.0.0/10,
# Tailscale's own range. Marcus accepted that trust level explicitly rather
# than asking for a token: only his own two phones and this PC are ever on
# that tailnet, the same guarantee Remote Desktop already relies on.
BIND_HOST = "0.0.0.0"


def list_faces():
    faces = []
    fdir = HERE / "faces"
    if fdir.is_dir():
        for p in sorted(fdir.iterdir()):
            if p.is_dir() and (p / "index.html").exists():
                meta = {"id": p.name, "title": p.name.title(), "tagline": ""}
                try:
                    meta.update(json.loads((p / "face.json").read_text()))
                except (OSError, ValueError):
                    pass
                meta["id"] = p.name
                faces.append(meta)
    return faces


PLANS_DIR = Path.home() / ".claude" / "plans"


def list_plans():
    """Plan Mode's own output, listed newest first — a viewer onto an
    existing workflow, not a new approval gate. Claude Code writes no
    record of whether a plan was approved, rejected, or abandoned
    anywhere this could find; this reports what was planned, not a
    verified approval trail. See the build plan's Phase 4 for the same
    caveat stated plainly rather than glossed over."""
    out = []
    if not PLANS_DIR.is_dir():
        return out
    for p in PLANS_DIR.glob("*.md"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        title = p.stem
        try:
            for line in p.read_text(encoding="utf-8",
                                    errors="ignore").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        except OSError:
            pass
        out.append({"file": p.name, "title": title, "mtime": mtime})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def mock_bus():
    t = time.time()
    level = 0.0
    samples = [0.0] * 64
    if MOCK == "speaking":
        level = abs(math.sin(t * 6.0)) * 0.85
        samples = [
            (math.sin(i * 0.55 + t * 9.0) * 0.6
             + math.sin(i * 1.7 - t * 13.0) * 0.4)
            * 9000.0 * (0.35 + 0.65 * abs(math.sin(t * 2.6)))
            for i in range(64)
        ]
    return {"state": MOCK, "level": level, "samples": samples,
            "alert": False, "loading": MOCK == "thinking",
            # Faked so the usage readout can be looked at without
            # spending a real session to make it appear.
            "rate_limits": {
                "five_hour": {"utilization": 0.34, "resets_at": t + 9200},
                "seven_day": {"utilization": 0.61, "resets_at": t + 288000},
            },
            "session_usage": {"turns": 7, "in_tokens": 41000,
                              "out_tokens": 6200, "cost": 0.38},
            "activity": "editing dashboard/index.html" if MOCK == "thinking"
                        else ""}


def read_bus():
    if MOCK:
        return mock_bus()
    try:
        state = (BUS / ".voice_state").read_text().strip().lower()
        if state not in STATES:
            state = "idle"
    except OSError:
        state = "idle"
    level = 0.0
    samples = [0.0] * 64
    try:
        payload = json.loads((BUS / ".voice_waveform").read_text())
        age = time.time() - float(payload.get("ts", 0))
        raw = payload.get("samples") or []
        if raw and age < WAVEFORM_STALE_S:
            # A fresh waveform IS speech, whatever the state file says.
            state = "speaking"
            samples = [float(s) for s in raw[:64]]
            mean = sum(abs(s) for s in samples) / len(samples)
            level = min(1.0, mean / 3000.0)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        alert = (BUS / ".voice_alert").stat().st_size > 0
    except OSError:
        alert = False
    loading = (BUS / ".voice_loading_pid").exists()
    activity = ""
    try:
        payload = json.loads((BUS / ".voice_activity").read_text())
        activity = payload.get("text") or ""
    except (OSError, ValueError):
        pass
    # Absent unless the voice line was told to publish it, which is the
    # normal case: it is the account holder's own spend and it stays off
    # until asked for. An empty dict simply means no readout.
    rate_limits = {}
    try:
        rate_limits = json.loads((BUS / ".voice_rate_limits").read_text())
    except (OSError, ValueError):
        pass
    # Same absent-unless-published posture as rate_limits: no file means
    # show_usage is off, or no turn has completed yet this session.
    session_usage = {}
    try:
        session_usage = json.loads(
            (BUS / ".voice_session_usage").read_text())
    except (OSError, ValueError):
        pass
    return {"state": state, "level": level, "samples": samples,
            "alert": alert, "loading": loading, "rate_limits": rate_limits,
            "session_usage": session_usage, "activity": activity}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/state":
                self._send(json.dumps(read_bus()).encode(),
                           "application/json")
            elif path == "/config":
                out = {"name": CFG["name"], "badge": CFG["badge"],
                       "face": CFG["face"],
                       "thinking_sound": bool(CFG["thinking_sound"]),
                       "faces": list_faces()}
                self._send(json.dumps(out).encode(), "application/json")
            elif path == "/sessions":
                self._send(
                    json.dumps(claude_sessions.list_sessions()).encode(),
                    "application/json")
            elif path == "/projects":
                self._send(
                    json.dumps(claude_sessions.list_projects()).encode(),
                    "application/json")
            elif path == "/plans":
                self._send(json.dumps(list_plans()).encode(),
                           "application/json")
            elif path == "/transcript":
                qs = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query)
                sid = (qs.get("session") or [""])[0]
                self._send(json.dumps(
                    claude_sessions.recent_messages(sid) if sid else []
                ).encode(), "application/json")
            elif path == "/plan":
                qs = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query)
                name = (qs.get("file") or [""])[0]
                # Bare filename only -- no separators, so this can never
                # walk outside PLANS_DIR regardless of what's passed.
                if not name or "/" in name or "\\" in name or name in (
                        ".", ".."):
                    self._send(b'{"error":"bad file"}',
                               "application/json", 400)
                else:
                    target = PLANS_DIR / name
                    if target.is_file():
                        self._send(json.dumps(
                            {"file": name,
                             "text": target.read_text(
                                 encoding="utf-8",
                                 errors="ignore")}).encode(),
                            "application/json")
                    else:
                        self._send(b'{"error":"not found"}',
                                   "application/json", 404)
            else:
                self._static(path)
        except ConnectionError:
            # THE WHOLE FAMILY, not one member of it. A tab closed or
            # reloaded mid-response raises ConnectionResetError, which is a
            # SIBLING of BrokenPipeError rather than a subclass -- so
            # catching only BrokenPipeError sent it to the generic branch
            # below, which then wrote a 500 back down the socket that had
            # just died and raised a SECOND, uncaught error from inside
            # flush_headers(). One disconnect, two tracebacks. ConnectionError
            # is the common parent of Reset, Broken, Aborted and Refused.
            pass
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            try:
                self._send(body, "application/json", 500)
            except ConnectionError:
                # A real error AND the client already gone. There is nobody
                # left to tell; saying so twice helps no one.
                pass

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        target = (HERE / path.lstrip("/")).resolve()
        if target != HERE and HERE not in target.parents:
            self._send(b"not found", "text/plain", 404)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(b"not found", "text/plain", 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or \
            "application/octet-stream"
        self._send(target.read_bytes(), ctype)

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _tailscale_ips() -> list[str]:
    """This machine's Tailscale v4 addresses, or [] if absent/not signed in.
    Same lookup shape as athena_desk.py's copy: PATH first, then where the
    installer actually puts it, since a shell opened before Tailscale was
    installed carries a stale PATH for its whole life."""
    exe = shutil.which("tailscale")
    if not exe:
        for candidate in (
            Path(os.environ.get("ProgramFiles", "")) / "Tailscale" / "tailscale.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Tailscale" / "tailscale.exe",
        ):
            if candidate.exists():
                exe = str(candidate)
                break
    if not exe:
        return []
    try:
        result = subprocess.run(
            [exe, "ip", "-4"], capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


if __name__ == "__main__":
    mode = f"MOCK={MOCK}" if MOCK else f"bus: {BUS}"
    root = f"http://127.0.0.1:{PORT}/"
    # The browser opens on the configured face; the gallery stays at "/" for switching.
    face = CFG.get("face", "")
    url = f"{root}faces/{face}/" if face and (HERE / "faces" / face / "index.html").exists() else root
    # ALREADY RUNNING IS NOT AN ERROR, and treating it as one was the whole
    # bug. Closing the browser tab does not stop this server; it keeps going
    # headless. Relaunching then failed to bind, died before the line that
    # opens the browser, and took the traceback with it when the launcher
    # window closed. The end-user symptom was "I can hear my agent but the
    # face never shows up", with the face running perfectly the entire time.
    try:
        srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    except OSError as e:
        if e.errno not in (errno.EADDRINUSE, errno.EACCES):
            raise
        # Something holds the port. Ask it whether it is us before claiming
        # anything: a stranger on this port is a different problem and
        # deserves a different sentence.
        mine = False
        try:
            with urllib.request.urlopen(root + "state", timeout=2) as r:
                mine = r.status == 200
        except Exception:
            mine = False
        if mine:
            print(f"already running at {root}  opening it instead", flush=True)
            if not NO_OPEN:
                webbrowser.open(url)
            sys.exit(0)
        print(f"port {PORT} is taken by something that is not this server.",
              flush=True)
        print("Close whatever is using it, or set a different \"port\" in "
              "ai-visualizer.json.", flush=True)
        sys.exit(1)
    srv.allow_reuse_address = True
    print(f"ai-visualizer on {root}  opening {url}  ({mode})  Ctrl-C stops", flush=True)
    for ip in _tailscale_ips():
        print(f"  also reachable at http://{ip}:{PORT}/  (phone, over Tailscale only)",
              flush=True)
    if not NO_OPEN:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
