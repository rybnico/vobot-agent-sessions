#!/usr/bin/env python3
"""Serve the state of running Claude Code sessions as JSON for the Agent Sessions Mini Dock app.

Claude Code keeps a live registry of its sessions in ``<claude dir>/sessions/<pid>.json``
(name, cwd, status, ...). This bridge reads those files, drops sessions whose process is
gone, looks up the session title from each transcript and serves the result on
``GET /sessions``. It also answers UDP discovery broadcasts so the device finds it without
configuration. Standard library only.
"""

import argparse
import glob
import hmac
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8765
DISCOVERY_REQUEST = b"agent-sessions/discover"
SHOWN_KINDS = {"interactive", "bg"}
STATUS_ORDER = {"waiting": 0, "busy": 1, "shell": 1, "idle": 2}
MAX_SESSIONS = 20
MAX_TEXT = 60

# The Mini Dock fonts cover ASCII, Latin, Greek and Cyrillic; fold common typography to ASCII
# and drop anything else (emoji, CJK) so the device never renders missing glyphs.
_FOLD = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
})

# (title, project, status, waiting_for, age in seconds) shown with --demo
DEMO_SESSIONS = [
    ("Add OAuth login to the web app", "webshop", "waiting", "input needed", 42),
    ("Fix flaky integration tests", "backend", "busy", "", 95),
    ("Run database migration", "backend", "shell", "", 310),
    ("Write release notes for 2.4", "docs", "idle", "", 1260),
    ("Refactor the payment module", "webshop", "idle", "", 7400),
]


def device_text(s, limit=MAX_TEXT):
    s = (s or "").translate(_FOLD)
    s = "".join(c for c in s if c >= " " and ord(c) <= 0x04FF).strip()
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


if os.name == "nt":
    import ctypes

    def pid_alive(pid):
        # os.kill(pid, 0) would terminate the process on Windows, so ask for its exit code instead
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
else:
    def pid_alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


class TitleCache:
    """Tracks the latest ai-title / custom-title per transcript, reading only appended bytes."""

    def __init__(self):
        self._paths = {}  # session id -> transcript path
        self._state = {}  # transcript path -> (offset, title)

    def title(self, claude_dir, session_id):
        path = self._paths.get(session_id)
        if not path or not os.path.exists(path):
            matches = glob.glob(os.path.join(claude_dir, "projects", "*", session_id + ".jsonl"))
            if not matches:
                return None
            path = self._paths[session_id] = matches[0]

        offset, title = self._state.get(path, (0, None))
        try:
            size = os.path.getsize(path)
            if size < offset:
                offset, title = 0, None
            if size > offset:
                with open(path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                end = chunk.rfind(b"\n") + 1  # leave a partially written last line for next time
                for line in chunk[:end].splitlines():
                    if b'-title"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("type") == "ai-title" and entry.get("aiTitle"):
                        title = entry["aiTitle"]
                    elif entry.get("type") == "custom-title" and entry.get("customTitle"):
                        title = entry["customTitle"]
                offset += end
        except OSError:
            return title
        self._state[path] = (offset, title)
        return title


def summarize(sessions):
    sessions.sort(key=lambda s: (STATUS_ORDER[s["status"]], -s.pop("_since")))
    counts = {"waiting": 0, "working": 0, "idle": 0}
    for s in sessions:
        counts["working" if s["status"] in ("busy", "shell") else s["status"]] += 1
    return {"sessions": sessions[:MAX_SESSIONS], "counts": counts, "total": len(sessions)}


def collect_sessions(claude_dirs, titles):
    now_ms = time.time() * 1000
    seen = {}
    for claude_dir in claude_dirs:
        for path in glob.glob(os.path.join(claude_dir, "sessions", "*.json")):
            try:
                with open(path) as f:
                    info = json.load(f)
            except (OSError, ValueError):
                continue
            sid = info.get("sessionId")
            pid = info.get("pid")
            if not sid or sid in seen or not isinstance(pid, int):
                continue
            if info.get("kind", "interactive") not in SHOWN_KINDS or not pid_alive(pid):
                continue
            status = info.get("status") or "idle"
            if status not in STATUS_ORDER:
                status = "idle"
            name = info.get("name") or ""
            title = name if info.get("nameSource") not in (None, "derived") else None
            title = title or titles.title(claude_dir, sid) or name or sid[:8]
            cwd = info.get("cwd") or ""
            since = info.get("statusUpdatedAt") or info.get("updatedAt") or info.get("startedAt") or now_ms
            seen[sid] = {
                "id": sid,
                "title": device_text(title),
                "name": device_text(name, 30),
                "project": device_text(os.path.basename(cwd.rstrip("/\\")) or cwd, 30),
                "status": status,
                "waiting_for": device_text(info.get("waitingFor") or "", 30),
                "age": max(0, int((now_ms - since) / 1000)),
                "_since": since,
            }
    return summarize(list(seen.values()))


def demo_sessions():
    now = time.time()
    return summarize([
        {"id": "demo-%d" % i, "title": title, "name": project, "project": project, "status": status,
         "waiting_for": waiting_for, "age": age, "_since": now - age}
        for i, (title, project, status, waiting_for, age) in enumerate(DEMO_SESSIONS)
    ])


def make_handler(source, token):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/", "/sessions"):
                self.send_error(404)
                return
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(401)
                return
            indent = 2 if self.path == "/" else None
            body = json.dumps(source(), ensure_ascii=False, indent=indent).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            if self.server.verbose:
                sys.stderr.write("%s %s - %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    return Handler


def serve_discovery(port, verbose):
    """Answer the app's UDP broadcast with the HTTP port; the app takes the address from the reply."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    reply = json.dumps({"service": "agent-sessions", "port": port}).encode()
    while True:
        try:
            data, addr = sock.recvfrom(256)
            if data.strip() == DISCOVERY_REQUEST:
                sock.sendto(reply, addr)
                if verbose:
                    sys.stderr.write("%s %s - discovery\n" % (time.strftime("%H:%M:%S"), addr[0]))
        except OSError as e:
            sys.stderr.write("discovery: %s\n" % e)
            time.sleep(1)


def find_claude_dirs(patterns):
    claude_dirs = []
    for pattern in patterns:
        for d in glob.glob(os.path.expanduser(pattern)) or [os.path.expanduser(pattern)]:
            d = os.path.realpath(d)
            if d not in claude_dirs and os.path.isdir(os.path.join(d, "sessions")):
                claude_dirs.append(d)
    return claude_dirs


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="HTTP and UDP discovery port (default: %d)" % DEFAULT_PORT)
    parser.add_argument("--claude-dir", action="append", dest="claude_dirs", metavar="DIR",
                        help="Claude config dir to scan; repeatable, globs allowed "
                             "(default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--token", default=os.environ.get("AGENT_SESSIONS_TOKEN"),
                        help="require this token from the device (default: $AGENT_SESSIONS_TOKEN)")
    parser.add_argument("--no-discovery", action="store_true", help="don't answer discovery broadcasts")
    parser.add_argument("--demo", action="store_true", help="serve made-up sessions instead of real ones")
    parser.add_argument("--once", action="store_true", help="print the current sessions and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = parser.parse_args()

    if args.demo:
        source, origin = demo_sessions, "demo data"
    else:
        claude_dirs = find_claude_dirs(args.claude_dirs or [os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"])
        if not claude_dirs:
            sys.exit("no Claude config dir with a sessions/ folder found; pass --claude-dir")
        titles = TitleCache()
        source, origin = (lambda: collect_sessions(claude_dirs, titles)), ", ".join(claude_dirs)

    if args.once:
        print(json.dumps(source(), ensure_ascii=False, indent=2))
        return

    server = ThreadingHTTPServer((args.host, args.port), make_handler(source, args.token))
    server.verbose = args.verbose
    if not args.no_discovery:
        threading.Thread(target=serve_discovery, args=(args.port, args.verbose), daemon=True).start()
    print("Serving sessions from %s on http://%s:%d/sessions%s"
          % (origin, args.host, args.port, " (token required)" if args.token else ""), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
