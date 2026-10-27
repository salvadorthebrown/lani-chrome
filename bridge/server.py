#!/usr/bin/env python3
"""Localhost bridge between a local model and the Chrome extension in ../extension.

Model side (any HTTP client):
  GET  /tools                 OpenAI-style tool schemas (tools.json)
  POST /cmd                   {"action": "...", "args": {...}} -> {"id", "ok", "result" | "error"}
Extension side:
  GET  /ext/poll[?ack=1]      next queued command, or 204 after ~20s with nothing queued
  POST /ext/ack               {"id"}: sent as soon as a command arrives (only when polling with ?ack=1)
  POST /ext/result            {"id", "ok", "result" | "error"}
  GET  /status                {"extension_connected": bool, "last_poll_age_s": float | null, "queued": int}
  GET  /cmd/<id>              what became of a command the caller stopped waiting on: answered |
                              not-delivered | waiting | queued | acknowledged-not-finished |
                              dispatched-not-acked | unknown

A command is never lost to a poll connection that died: before handing one out the bridge checks the
socket is still open, a failed write puts the command back, and with ?ack=1 a command that is not
acknowledged within ACK_TIMEOUT_S is queued again. (Chrome suspends extension service workers, which
drops their pending poll; before this, the next command went to that dead socket and the caller sat
out its whole timeout.)

Delivery is deliberately at-most-once once acknowledged, which means a caller whose wait expired cannot
tell "it never ran" from "it ran and I missed the reply" — and retrying a click in that doubt runs it
twice. So the bridge keeps the last outcomes and says which of the three states a timed-out command was
in, and GET /cmd/<id> answers the question afterwards. A result that arrives after the caller gave up is
kept and handed back as the command's outcome rather than discarded.

Every request needs `Authorization: Bearer <token>`. The token is created on first run at
~/.config/chrome-tab-bridge/token (override with TAB_BRIDGE_TOKEN_FILE). Any request carrying an
Origin other than chrome-extension:// is refused, so ordinary web pages cannot reach the bridge.
"""
import argparse
import collections
import json
import os
import pathlib
import secrets
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
TOKEN_PATH = pathlib.Path(
    os.environ.get("TAB_BRIDGE_TOKEN_FILE") or pathlib.Path.home() / ".config/chrome-tab-bridge/token"
)
POLL_HOLD_S = 20  # under Chrome's 30s limit for a pending fetch in an extension service worker
ACK_TIMEOUT_S = 5
DEFAULT_CMD_TIMEOUT_S = 60
EXTENSION_IDLE_S = 40  # gap between polls after which the extension counts as gone
SETTLED_KEEP_S = 900   # how long a finished command's outcome stays queryable at GET /cmd/<id>
SETTLED_MAX = 400


def load_or_create_token():
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


class Bridge:
    def __init__(self):
        self.commands = collections.deque()
        self.waiters = {}        # cmd id -> {"event", "reply"}
        self.unacked = {}        # cmd id -> (cmd, requeue deadline) for pollers that promised an ack
        # Used as ordered sets so a bridge that has been up for weeks shedding timed-out commands does
        # not grow one id per timeout forever. Membership reads the same as a set's.
        self.dispatched = collections.OrderedDict()  # cmd ids that left the queue, even if the write failed
        self.acked = collections.OrderedDict()       # ... and the worker confirmed taking it, so it probably ran
        self.settled = collections.OrderedDict()  # cmd id -> {"reply", "at"}, newest last
        self.cond = threading.Condition()
        self.last_poll = 0.0

    @staticmethod
    def _mark(ordered_ids, cmd_id, keep=2000):
        ordered_ids[cmd_id] = True
        ordered_ids.move_to_end(cmd_id)
        while len(ordered_ids) > keep:
            ordered_ids.popitem(last=False)

    def _remember(self, cmd_id, reply=None, not_delivered=False):
        """Keep finished outcomes so a caller that gave up can still ask what happened."""
        self.settled[cmd_id] = {"reply": reply, "not_delivered": not_delivered, "at": time.time()}
        self.settled.move_to_end(cmd_id)
        cutoff = time.time() - SETTLED_KEEP_S
        while self.settled and (len(self.settled) > SETTLED_MAX
                                or next(iter(self.settled.values()))["at"] < cutoff):
            self.settled.popitem(last=False)

    def submit(self, action, args, timeout):
        cmd_id = uuid.uuid4().hex
        waiter = {"event": threading.Event(), "reply": None}
        with self.cond:
            self.waiters[cmd_id] = waiter
            self.commands.append({"id": cmd_id, "action": action, "args": args})
            self.cond.notify()
        done = waiter["event"].wait(timeout)
        with self.cond:
            self.waiters.pop(cmd_id, None)
            self.unacked.pop(cmd_id, None)
            self.commands = collections.deque(c for c in self.commands if c["id"] != cmd_id)
            # A result that landed while we were tearing down is a real answer, not a timeout.
            late = self.settled.get(cmd_id)
            if not done and late is not None and late.get("reply") is not None:
                return dict(late["reply"])
            dispatched, acked = cmd_id in self.dispatched, cmd_id in self.acked
            if not done and not dispatched:
                self._remember(cmd_id, not_delivered=True)  # nothing handed this off, so we can be sure
        if done:
            return waiter["reply"]
        age = time.time() - self.last_poll if self.last_poll else None
        seen = "the extension has never polled" if age is None else f"the extension last polled {age:.0f}s ago"
        # The caller is about to decide whether to retry, which double-fires a click if this did run.
        # Say which of the three states we are actually in instead of making them guess.
        if acked:
            state = (f"it WAS ACKNOWLEDGED by the extension, so the action may well have run — "
                     f"check GET /cmd/{cmd_id} before retrying")
        elif dispatched:
            state = (f"it was handed to the extension but never acknowledged, so it most likely did "
                     f"not run; GET /cmd/{cmd_id} confirms")
        else:
            state = f"it never reached the extension, so nothing ran and retrying is safe (GET /cmd/{cmd_id})"
        return {
            "id": cmd_id,
            "ok": False,
            "error": f"no reply from the extension after {timeout:g}s ({seen}); {state}. "
                     "Is Chrome running with Lani's bridge connection turned on?",
        }

    def outcome(self, cmd_id):
        """What became of a command, for a caller that stopped waiting on it."""
        with self.cond:
            rec = self.settled.get(cmd_id)
            if rec is not None:
                if rec.get("not_delivered"):
                    return {"id": cmd_id, "status": "not-delivered",
                            "note": "the caller gave up before the extension took it; nothing ran"}
                return {"id": cmd_id, "status": "answered", "reply": rec["reply"], "at": rec["at"]}
            if cmd_id in self.waiters:
                return {"id": cmd_id, "status": "waiting"}
            if any(c["id"] == cmd_id for c in self.commands):
                return {"id": cmd_id, "status": "queued"}
            if cmd_id in self.acked:
                return {"id": cmd_id, "status": "acknowledged-not-finished",
                        "note": "the worker took it and never reported back; assume the action ran"}
            if cmd_id in self.dispatched:
                return {"id": cmd_id, "status": "dispatched-not-acked",
                        "note": "handed to the worker without an ack; it most likely did not run"}
            return {"id": cmd_id, "status": "unknown",
                    "note": "no record of this id; older than the outcome cache, or never submitted"}

    def _requeue_expired(self):
        now = time.time()
        for cmd_id, (cmd, deadline) in list(self.unacked.items()):
            if now >= deadline:
                del self.unacked[cmd_id]
                if cmd_id in self.waiters:
                    self.commands.appendleft(cmd)

    def next_command(self, hold, alive, wants_ack):
        """Waits up to `hold` seconds for a command for a poller whose connection `alive()` reports on."""
        self.last_poll = time.time()
        deadline = time.time() + hold
        with self.cond:
            while time.time() < deadline:
                self._requeue_expired()
                if self.commands:
                    if not alive():
                        return None  # the poller hung up; leave the command for the next one
                    cmd = self.commands.popleft()
                    self._mark(self.dispatched, cmd["id"])
                    if wants_ack:
                        self.unacked[cmd["id"]] = (cmd, time.time() + ACK_TIMEOUT_S)
                    return cmd
                self.cond.wait(0.5)
        return None

    def requeue(self, cmd):
        with self.cond:
            self.unacked.pop(cmd["id"], None)
            if cmd["id"] in self.waiters:
                self.commands.appendleft(cmd)
                self.cond.notify()

    def ack(self, cmd_id):
        with self.cond:
            self.unacked.pop(cmd_id, None)
            self._mark(self.acked, cmd_id)  # from here on the action counts as probably-run

    def complete(self, reply):
        cmd_id = reply.get("id")
        with self.cond:
            if cmd_id is not None:
                self._remember(cmd_id, reply)
            self.unacked.pop(cmd_id, None)
            self.dispatched.pop(cmd_id, None)
            self.acked.pop(cmd_id, None)
            waiter = self.waiters.get(cmd_id)
        if waiter:
            waiter["reply"] = reply
            waiter["event"].set()

    def queued(self):
        with self.cond:
            return len(self.commands)


class Handler(BaseHTTPRequestHandler):
    server_version = "TabBridge/0.1"
    bridge = None
    tools = None
    token = None

    def log_message(self, fmt, *args):
        if os.environ.get("TAB_BRIDGE_DEBUG"):
            super().log_message(fmt, *args)

    def send_json(self, code, payload=None):
        self.send_response(code)
        if payload is not None:
            body = json.dumps(payload).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if payload is not None:
            self.wfile.write(body)
        self.wfile.flush()

    def peer_open(self):
        """False once the client has closed its end (for example a suspended service worker)."""
        try:
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) != b""
        except BlockingIOError:
            return True
        except OSError:
            return False

    def allowed(self):
        origin = self.headers.get("Origin")
        if origin and not origin.startswith("chrome-extension://"):
            return False
        supplied = self.headers.get("Authorization", "").encode()
        return secrets.compare_digest(supplied, f"Bearer {self.token}".encode())

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if not self.allowed():
            return self.send_json(401, {"error": "unauthorized"})
        path, _, query = self.path.partition("?")
        if path == "/ext/poll":
            cmd = self.bridge.next_command(POLL_HOLD_S, self.peer_open, "ack=1" in query.split("&"))
            if cmd is None:
                return self.send_json(204)
            try:
                return self.send_json(200, cmd)
            except OSError:
                self.bridge.requeue(cmd)
                raise
        if path.startswith("/cmd/"):
            # For a caller that timed out and needs to know whether its click actually landed.
            return self.send_json(200, self.bridge.outcome(path.split("/", 2)[2]))
        if path == "/tools":
            return self.send_json(200, self.tools)
        if path == "/status":
            age = time.time() - self.bridge.last_poll if self.bridge.last_poll else None
            return self.send_json(200, {"extension_connected": age is not None and age < EXTENSION_IDLE_S,
                                        "last_poll_age_s": None if age is None else round(age, 1),
                                        "queued": self.bridge.queued()})
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return self.send_json(401, {"error": "unauthorized"})
        path = self.path.split("?")[0]
        try:
            body = self.read_json()
        except ValueError:
            return self.send_json(400, {"error": "body must be JSON"})
        if path == "/ext/ack":
            self.bridge.ack(body.get("id"))
            return self.send_json(200, {"ok": True})
        if path == "/ext/result":
            self.bridge.complete(body)
            return self.send_json(200, {"ok": True})
        if path == "/cmd":
            action = body.get("action")
            if not action:
                return self.send_json(400, {"error": "missing 'action'"})
            timeout = min(max(float(body.get("timeout_s", DEFAULT_CMD_TIMEOUT_S)), 1), 300)
            return self.send_json(200, self.bridge.submit(action, body.get("args") or {}, timeout))
        self.send_json(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return  # the client hung up; nothing to do
        super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description="Localhost bridge for the Chrome tab-driver extension.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        raise SystemExit("refusing to bind anything but loopback: this bridge can drive your browser")

    Handler.bridge = Bridge()
    Handler.tools = json.loads((HERE.parent / "extension" / "tools.json").read_text())
    Handler.token = load_or_create_token()

    server = Server((args.host, args.port), Handler)
    print(f"tab bridge listening on http://{args.host}:{args.port}")
    print(f"token file: {TOKEN_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
