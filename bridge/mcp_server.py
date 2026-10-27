#!/usr/bin/env python3
"""MCP (stdio) server that exposes the Chrome tab bridge as tools for Claude Code.

Claude Code starts this process and talks JSON-RPC over stdin/stdout. Every tool call is
forwarded to the bridge's POST /cmd, so the bridge (server.py) must be running.
Stdlib only; tool schemas come from tools.json.
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
BRIDGE = os.environ.get("TAB_BRIDGE_URL", "http://127.0.0.1:8765")
TOKEN_PATH = pathlib.Path(
    os.environ.get("TAB_BRIDGE_TOKEN_FILE") or pathlib.Path.home() / ".config/chrome-tab-bridge/token"
)
TOOLS = [
    {"name": t["function"]["name"],
     "description": t["function"]["description"],
     "inputSchema": t["function"]["parameters"]}
    for t in json.loads((HERE.parent / "extension" / "tools.json").read_text())
]
PROTOCOL = "2025-06-18"


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


LATE_RESULT_WAIT_S = 20  # keep asking this long after the bridge's own wait expired


def auth():
    return {"Authorization": f"Bearer {TOKEN_PATH.read_text().strip()}"}


def call_bridge(action, args):
    req = urllib.request.Request(
        f"{BRIDGE}/cmd",
        data=json.dumps({"action": action, "args": args}).encode(),
        headers={**auth(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


def late_result(cmd_id, wait_s=LATE_RESULT_WAIT_S):
    """The action may have finished just after the caller stopped waiting; the bridge kept the reply."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{BRIDGE}/cmd/{cmd_id}", headers=auth())
            with urllib.request.urlopen(req, timeout=10) as resp:
                rec = json.loads(resp.read())
        except (urllib.error.URLError, OSError):
            return None
        if rec.get("status") == "answered":
            return rec.get("reply")
        if rec.get("status") in ("unknown", "not-delivered"):
            return None  # settled against us: nothing more will ever arrive for this id
        time.sleep(0.5)
    return None


def run_tool(name, args):
    try:
        reply = call_bridge(name, args)
    except (urllib.error.URLError, OSError) as err:
        return [{"type": "text", "text": f"bridge unreachable at {BRIDGE}: {err}. Is server.py running?"}], True
    note = ""
    if not reply.get("ok") and reply.get("id"):
        # An expired wait says nothing about whether the action ran, and retrying a click that already
        # ran is the failure that costs real money here. Go and read the outcome instead of guessing.
        settled = late_result(reply["id"])
        if settled is not None:
            reply = settled
            note = ("note: this action completed after the caller's wait had already expired, so the "
                    "result below arrived late rather than being lost.")
    if not reply.get("ok"):
        hint = ""
        if reply.get("id") and "no reply from the extension" in str(reply.get("error")):
            hint = (f" [command id {reply['id']}: ask the bridge `GET /cmd/{reply['id']}` before "
                    "retrying — an action can run without replying]")
        return [{"type": "text", "text": f"error: {reply.get('error')}{hint}"}], True
    result = reply.get("result")
    content = []
    if isinstance(result, dict) and "png_base64" in result:
        content.append({"type": "image", "data": result.pop("png_base64"), "mimeType": "image/png"})
    content.append({"type": "text", "text": json.dumps(result, ensure_ascii=False)})
    if note:
        content.append({"type": "text", "text": note})
    return content, False


def handle(msg):
    method = msg.get("method")
    req_id = msg.get("id")
    if req_id is None:
        return  # notifications (e.g. notifications/initialized) need no reply
    if method == "initialize":
        result = {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "chrome-tab-bridge", "version": "0.1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params", {})
        content, is_error = run_tool(params.get("name", ""), params.get("arguments") or {})
        result = {"content": content, "isError": is_error}
    else:
        send({"jsonrpc": "2.0", "id": req_id,
              "error": {"code": -32601, "message": f"method not found: {method}"}})
        return
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            continue


if __name__ == "__main__":
    main()
