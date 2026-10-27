#!/usr/bin/env python3
"""Bridge delivery tests, no browser needed: commands must survive a poller that dies mid-poll.

Chrome suspends extension service workers, which drops their pending /ext/poll. These cases pin the
failure seen live on 2026-10-25 (the next command went to the dead poll and the caller waited 60s).

    python3 tests/bridge_reconnect_test.py
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    failures += 0 if ok else 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bridge:
    def __enter__(self):
        self.port = free_port()
        tok = tempfile.NamedTemporaryFile("w", delete=False)
        tok.write(TOKEN + "\n")
        tok.close()
        self.token_file = tok.name
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge" / "server.py"), "--port", str(self.port)],
            env=dict(os.environ, TAB_BRIDGE_TOKEN_FILE=tok.name),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        self.proc.kill()
        self.stderr = self.proc.stderr.read().decode()
        os.unlink(self.token_file)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path, body, timeout=20):
        req = urllib.request.Request(self.url(path), method="POST", data=json.dumps(body).encode(), headers=H)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

    def get(self, path, timeout=15):
        req = urllib.request.Request(self.url(path), headers=H)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

    def cmd(self, action, timeout_s):
        start = time.time()
        reply = self.post("/cmd", {"action": action, "timeout_s": timeout_s}, timeout=timeout_s + 10)
        return reply, time.time() - start

    def dead_poll(self, query=""):
        """Starts a poll and hangs up, as a suspended service worker does."""
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(f"GET /ext/poll{query} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
        time.sleep(0.1)
        s.close()

    def good_poller(self, ack, stop):
        def run():
            while not stop.is_set():
                try:
                    r = urllib.request.urlopen(urllib.request.Request(
                        self.url("/ext/poll" + ("?ack=1" if ack else "")), headers=H), timeout=40)
                except Exception:
                    return
                if r.status == 200:
                    cmd = json.loads(r.read())
                    if ack:
                        self.post("/ext/ack", {"id": cmd["id"]})
                    self.post("/ext/result", {"id": cmd["id"], "ok": True, "result": cmd["action"]})
        threading.Thread(target=run, daemon=True).start()


# 1. Pollers that hung up before a command arrived must not swallow it.
with Bridge() as b:
    for _ in range(3):
        b.dead_poll()
    stop = threading.Event()
    b.good_poller(ack=False, stop=stop)
    time.sleep(0.3)
    reply, took = b.cmd("tabs_list", 8)
    stop.set()
    check("command survives pollers that hung up", reply.get("ok") and took < 3, f"{reply} in {took:.1f}s")
check("a hung-up poller leaves no traceback in the log", "Traceback" not in b.stderr, b.stderr[-300:])

# 2. A poller that took a command (with ?ack=1) and died before acking: the command goes to the next poller.
with Bridge() as b:
    got = {}

    def taker():
        r = urllib.request.urlopen(urllib.request.Request(b.url("/ext/poll?ack=1"), headers=H), timeout=40)
        got["cmd"] = json.loads(r.read())  # read it, then vanish without acking

    t = threading.Thread(target=taker, daemon=True)
    t.start()
    time.sleep(0.3)
    result = {}
    caller = threading.Thread(target=lambda: result.update(zip(("reply", "took"), b.cmd("snapshot", 20))))
    caller.start()
    t.join(5)
    stop = threading.Event()
    b.good_poller(ack=True, stop=stop)
    caller.join(25)
    stop.set()
    check("first poller received the command", "cmd" in got)
    check("unacked command is redelivered to the next poller",
          result.get("reply", {}).get("ok") and result["took"] < 12, str(result))

# 3. An acked command is not redelivered (no double clicks).
with Bridge() as b:
    seen = []

    def counting_poller():
        while len(seen) < 2:
            try:
                r = urllib.request.urlopen(urllib.request.Request(b.url("/ext/poll?ack=1"), headers=H), timeout=40)
            except Exception:
                return
            if r.status == 200:
                cmd = json.loads(r.read())
                seen.append(cmd["id"])
                b.post("/ext/ack", {"id": cmd["id"]})
                time.sleep(7)  # a slow action, longer than the ack window
                b.post("/ext/result", {"id": cmd["id"], "ok": True, "result": "slow"})

    threading.Thread(target=counting_poller, daemon=True).start()
    threading.Thread(target=counting_poller, daemon=True).start()
    time.sleep(0.3)
    reply, took = b.cmd("click", 20)
    time.sleep(1)
    check("slow acked command completes", reply.get("ok"), str(reply))
    check("acked command ran exactly once", len(seen) == 1, f"delivered {len(seen)} times")

# 4. With no extension at all, the error says when it was last seen.
with Bridge() as b:
    reply, took = b.cmd("tabs_list", 1)
    check("no-extension error says it never polled", not reply["ok"] and "never polled" in reply["error"], str(reply))
    status = json.loads(urllib.request.urlopen(urllib.request.Request(b.url("/status"), headers=H)).read())
    check("status reports last_poll_age_s", "last_poll_age_s" in status and status["extension_connected"] is False, str(status))

# 5. Nothing ever took the command, so the bridge can promise a retry is safe.
with Bridge() as b:
    reply, took = b.cmd("click", 1)
    check("undelivered timeout says retrying is safe",
          not reply["ok"] and "never reached the extension" in reply["error"], str(reply))
    rec = b.get(f"/cmd/{reply['id']}")
    check("undelivered command resolves as not-delivered", rec.get("status") == "not-delivered", str(rec))

# 6. The worker took the command and died: the caller must NOT be told a retry is safe.
with Bridge() as b:
    def ack_and_vanish():
        r = urllib.request.urlopen(urllib.request.Request(b.url("/ext/poll?ack=1"), headers=H), timeout=40)
        cmd = json.loads(r.read())
        b.post("/ext/ack", {"id": cmd["id"]})  # it is taken; the worker dies here and never answers

    threading.Thread(target=ack_and_vanish, daemon=True).start()
    time.sleep(0.3)
    reply, took = b.cmd("click", 2)
    check("acked-then-silent timeout warns the action may have run",
          not reply["ok"] and "ACKNOWLEDGED" in reply["error"], str(reply))
    rec = b.get(f"/cmd/{reply['id']}")
    check("acked-then-silent command is not offered as safe to retry",
          rec.get("status") == "acknowledged-not-finished", str(rec))

# 7. An action that finishes just after the caller gave up is kept, not thrown away.
with Bridge() as b:
    def slow_worker():
        r = urllib.request.urlopen(urllib.request.Request(b.url("/ext/poll?ack=1"), headers=H), timeout=40)
        cmd = json.loads(r.read())
        b.post("/ext/ack", {"id": cmd["id"]})
        time.sleep(3)  # the action outlives the caller's patience
        b.post("/ext/result", {"id": cmd["id"], "ok": True, "result": "landed"})

    threading.Thread(target=slow_worker, daemon=True).start()
    time.sleep(0.3)
    reply, took = b.cmd("click", 1)
    check("an action slower than the caller's wait does time out", not reply["ok"], str(reply))
    time.sleep(3)
    rec = b.get(f"/cmd/{reply['id']}")
    check("late result is remembered instead of discarded",
          rec.get("status") == "answered" and rec.get("reply", {}).get("result") == "landed", str(rec))

print("\nall bridge reconnect checks passed" if not failures else f"\n{failures} check(s) failed")
sys.exit(1 if failures else 0)
