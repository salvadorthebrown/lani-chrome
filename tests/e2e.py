#!/usr/bin/env python3
"""End-to-end test: loads the real extension into Chromium and drives it.

Covers the side-panel agent (both reply styles, against a scripted fake model server), cross-origin
iframe support, and the external-agent bridge. Needs Playwright and a Chromium that supports extensions.

  LANI_CHROME=/path/to/chromium python3 tests/e2e.py
"""
import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
CHROME = os.environ.get("LANI_CHROME") or str(
    pathlib.Path.home()
    / "Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64"
    / "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
)

PAGE_PORT, FRAME_PORT, LLM_PORT, BRIDGE_PORT = 9101, 9102, 9103, 9111
PAGE_URL = f"http://127.0.0.1:{PAGE_PORT}/"

PAGE = f"""<!doctype html><title>Lani Test</title>
<label for=name>Name</label> <input id=name>
<button onclick="document.getElementById('out').textContent='Hello '+document.getElementById('name').value">Greet</button>
<p id=out>nothing yet</p><p id=out2>no frame click</p>
<iframe src="http://localhost:{FRAME_PORT}/frame.html" width=300 height=80></iframe>
<script>addEventListener('message', e => {{ document.getElementById('out2').textContent = e.data }})</script>"""
FRAME = """<!doctype html><title>frame</title>
<button onclick="parent.postMessage('frame clicked', '*')">Frame button</button>"""

failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def serve_html(port, body):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever, daemon=True).start()


# ---- fake model server: reads the page view in the last message and picks the next action --------

llm_requests = []
violations = []  # malformed Anthropic requests the fake server saw


def decide(view):
    def ref(label):
        for line in view.splitlines():
            m = re.match(r'\[(\d+)\] .*"%s"' % re.escape(label), line)
            if m:
                return int(m.group(1))

    name_line = next((l for l in view.splitlines() if re.match(r"\[\d+\] input", l)), "")
    if '= "Lani"' not in name_line:
        return "type", {"ref": ref("Name"), "text": "Lani"}
    if "Hello Lani" not in view:
        return "click", {"ref": ref("Greet")}
    if "frame clicked" not in view:
        return "click", {"ref": ref("Frame button")}
    return "done", {"answer": "greeted and clicked the frame button"}


def serve_llm():
    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send({"data": [{"id": "fake"}]})

        def anthropic(self, body):
            msgs = body["messages"]
            if msgs[0]["role"] != "user" or any(a["role"] == b["role"] for a, b in zip(msgs, msgs[1:])):
                violations.append("roles do not alternate")
            if any(not m["content"] for m in msgs):
                violations.append("empty content")
            if not isinstance(body.get("system"), str) or not body.get("max_tokens"):
                violations.append("missing system or max_tokens")
            llm_requests.append({"tools": "tools" in body, "messages": [
                {"role": m["role"], "content": " ".join(b.get("text", "") for b in m["content"])} for m in msgs]})
            view = "\n".join(b["text"] for b in msgs[-1]["content"] if b["type"] == "text")
            action, args = decide(view)
            if "tools" in body:
                if action == "done":
                    content = [{"type": "text", "text": args["answer"]}]
                else:
                    content = [{"type": "tool_use", "id": f"toolu_{len(llm_requests)}", "name": action, "input": args}]
            else:
                if action == "type":
                    text = f"type {args['ref']} | {args['text']}"
                elif action == "click":
                    text = f"click {args['ref']}"
                else:
                    text = f"done | {args['answer']}"
                content = [{"type": "text", "text": text}]
            self._send({"type": "message", "role": "assistant", "content": content})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/messages"):
                return self.anthropic(body)
            messages = body["messages"]
            llm_requests.append({"tools": "tools" in body, "messages": messages})
            action, args = decide(messages[-1]["content"])
            if body.get("model") == "looper":  # a stuck model: the same command forever
                return self._send({"choices": [{"message": {"role": "assistant", "content": "read 1"}}]})
            if "tools" in body:
                if action == "done":
                    msg = {"role": "assistant", "content": args["answer"]}
                else:
                    msg = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call_{len(llm_requests)}", "type": "function",
                        "function": {"name": action, "arguments": json.dumps(args)}}]}
            else:
                if action == "type":
                    text = f"type {args['ref']} | {args['text']}"
                elif action == "click":
                    text = f"click {args['ref']}"
                else:
                    text = f"done | {args['answer']}"
                if len(llm_requests) % 5 == 1:  # small models ramble: prose before the command
                    text = f"I should do the next step.\n```\n{text}\n```"
                msg = {"role": "assistant", "content": text}
            self._send({"choices": [{"message": msg}]})

        def log_message(self, *args):
            pass

    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", LLM_PORT), Handler).serve_forever, daemon=True).start()


def bridge_call(token, action, args=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{BRIDGE_PORT}/cmd",
        data=json.dumps({"action": action, "args": args or {}, "timeout_s": 30}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=40).read())


async def run_panel_task(ctx, ext_id, page, mode, api="openai"):
    """Opens the side panel as a tab, points it at the fake model, runs the task, returns (panel, final text)."""
    await page.goto(PAGE_URL)
    panel = await ctx.new_page()
    errors = []
    panel.on("pageerror", lambda err: errors.append(str(err)))
    await panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
    await panel.evaluate(
        "o => chrome.storage.local.set(o)",
        {"baseUrl": f"http://127.0.0.1:{LLM_PORT}" + ("" if api == "anthropic" else "/v1"),
         "model": "fake", "mode": mode, "api": api, "maxSteps": 10},
    )
    await panel.reload()
    await panel.wait_for_selector("#dot.ok", timeout=8000)
    await panel.fill("#task", "Greet Lani and click the frame button")
    await panel.press("#task", "Enter")
    await panel.wait_for_selector(".bubble.final, .bubble.error", timeout=60000)
    final = await panel.inner_text(".bubble.final, .bubble.error")
    return panel, final, errors


async def main():
    serve_html(PAGE_PORT, PAGE)
    serve_html(FRAME_PORT, FRAME)
    serve_llm()
    token_file = pathlib.Path(tempfile.mkdtemp(prefix="lani-token-")) / "token"
    bridge = subprocess.Popen(
        [sys.executable, str(ROOT / "bridge" / "server.py"), "--port", str(BRIDGE_PORT)],
        env={**os.environ, "TAB_BRIDGE_TOKEN_FILE": str(token_file)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    token = token_file.read_text().strip()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="lani-e2e-"),
            executable_path=CHROME,
            headless=False,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--headless=new", "--no-first-run"],
        )
        try:
            worker = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker", timeout=15000)
            ext_id = worker.url.split("/")[2]
            check("extension loaded (service worker started)", worker.url.endswith("background.js"), worker.url)

            page = await ctx.new_page()

            # 1. Side-panel agent, one-line command style.
            panel, final, errors = await run_panel_task(ctx, ext_id, page, "text")
            check("text mode: agent finished with the model's answer", final == "greeted and clicked the frame button", final)
            check("text mode: typed and clicked on the real page", await page.inner_text("#out") == "Hello Lani")
            check("text mode: clicked a button inside a cross-origin iframe", await page.inner_text("#out2") == "frame clicked")
            check("text mode: three steps shown", await panel.locator(".step").count() == 3, str(await panel.locator(".step").count()))
            check("text mode: trace download offered", await panel.is_visible("#trace"))
            check("text mode: older page views were shrunk", any(
                "[older page view omitted]" in m["content"]
                for r in llm_requests[-1:] for m in r["messages"] if isinstance(m.get("content"), str)))
            check("text mode: no JavaScript errors in the panel", not errors, "; ".join(errors))
            await panel.close()

            # 2. Side-panel agent, tool-calling style.
            llm_requests.clear()
            panel, final, errors = await run_panel_task(ctx, ext_id, page, "tools")
            check("tools mode: agent finished with the model's answer", final == "greeted and clicked the frame button", final)
            check("tools mode: acted on the page and the iframe",
                  await page.inner_text("#out") == "Hello Lani" and await page.inner_text("#out2") == "frame clicked")
            check("tools mode: request carried tool schemas", all(r["tools"] for r in llm_requests))
            check("tools mode: no JavaScript errors in the panel", not errors, "; ".join(errors))

            # 2b. Same two reply styles over the Anthropic Messages format (what the claude-local router speaks).
            for mode in ("text", "tools"):
                llm_requests.clear()
                panel, final, errors = await run_panel_task(ctx, ext_id, page, mode, api="anthropic")
                check(f"anthropic format, {mode} mode: agent finished with the model's answer",
                      final == "greeted and clicked the frame button", final)
                check(f"anthropic format, {mode} mode: acted on the page and the iframe",
                      await page.inner_text("#out") == "Hello Lani" and await page.inner_text("#out2") == "frame clicked")
                check(f"anthropic format, {mode} mode: every request was well-formed", not violations, "; ".join(violations))
                check(f"anthropic format, {mode} mode: no JavaScript errors in the panel", not errors, "; ".join(errors))
                await panel.close()

            # 2c. A model that repeats itself is stopped with a clear message instead of burning every step.
            await page.goto(PAGE_URL)
            panel = await ctx.new_page()
            await panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            await panel.evaluate("o => chrome.storage.local.set(o)", {
                "baseUrl": f"http://127.0.0.1:{LLM_PORT}/v1", "model": "looper", "mode": "text", "api": "openai", "maxSteps": 20})
            await panel.reload()
            await panel.fill("#task", "anything")
            await panel.press("#task", "Enter")
            await panel.wait_for_selector(".bubble.error", timeout=30000)
            error = await panel.inner_text(".bubble.error")
            check("a repeating model is stopped with a clear message", "kept repeating" in error and "read 1" in error, error)
            check("repeat stop happens early, not at the step limit", await panel.locator(".step").count() <= 5,
                  str(await panel.locator(".step").count()))
            await panel.close()

            # 3. Stop button and unreachable model.
            panel = await ctx.new_page()
            await panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            await panel.evaluate("o => chrome.storage.local.set(o)", {"baseUrl": "http://127.0.0.1:9/v1", "api": "openai"})
            await panel.reload()
            await panel.wait_for_selector("#dot.bad", timeout=8000)
            check("status dot goes red when the model server is down", True)
            await panel.fill("#task", "anything")
            await panel.press("#task", "Enter")
            await panel.wait_for_selector(".bubble.error", timeout=15000)
            check("unreachable model shows an error bubble", True)
            await panel.close()

            # 4. External-agent bridge.
            helper = await ctx.new_page()
            await helper.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            await helper.evaluate("o => chrome.storage.local.set(o)", {
                "bridgeEnabled": True, "bridgeUrl": f"http://127.0.0.1:{BRIDGE_PORT}", "bridgeToken": token})
            for _ in range(40):
                status = json.loads(urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{BRIDGE_PORT}/status", headers={"Authorization": f"Bearer {token}"})).read())
                if status["extension_connected"]:
                    break
                await asyncio.sleep(0.5)
            check("bridge: extension connected after enabling it in settings", status["extension_connected"])

            await page.goto(PAGE_URL)
            tabs = (await asyncio.to_thread(bridge_call, token, "tabs_list"))["result"]
            tab_id = next(t["tab_id"] for t in tabs if t["url"] == PAGE_URL)
            snap = (await asyncio.to_thread(bridge_call, token, "snapshot", {"tab_id": tab_id}))["result"]
            frame_btn = next((e for e in snap["elements"] if e["label"] == "Frame button"), None)
            check("bridge: snapshot lists the iframe button with its frame", bool(frame_btn) and frame_btn.get("frame") == f"localhost:{FRAME_PORT}", str(frame_btn))
            check("bridge: refs are numbered across frames", [e["ref"] for e in snap["elements"]] == list(range(1, len(snap["elements"]) + 1)))
            reply = await asyncio.to_thread(bridge_call, token, "click", {"tab_id": tab_id, "ref": frame_btn["ref"]})
            check("bridge: click routed into the iframe", reply["ok"] and await page.inner_text("#out2") == "frame clicked", str(reply))

            reply = await asyncio.to_thread(bridge_call, token, "no_such_action")
            check("bridge: unknown action lists the valid ones", not reply["ok"] and "Valid actions:" in reply["error"] and "snapshot" in reply["error"], str(reply))
            reply = await asyncio.to_thread(bridge_call, token, "click", {"tab_id": tab_id, "ref": 999})
            check("bridge: stale ref gives a clear error", not reply["ok"] and "take a new snapshot" in reply["error"], str(reply))
            reply = await asyncio.to_thread(bridge_call, token, "eval_js", {"tab_id": tab_id, "code": "document.title"})
            check("bridge: eval_js is off by default", not reply["ok"] and "turned off" in reply["error"], str(reply))
            await helper.evaluate("o => chrome.storage.local.set(o)", {"allowEval": True})
            reply = await asyncio.to_thread(bridge_call, token, "eval_js", {"tab_id": tab_id, "code": "document.title"})
            check("bridge: eval_js works once enabled", reply["ok"] and reply["result"]["value"] == "Lani Test", str(reply))
            reply = await asyncio.to_thread(bridge_call, token, "screenshot", {"tab_id": tab_id})
            check("bridge: screenshot returns a PNG", reply["ok"] and reply["result"]["png_base64"].startswith("iVBOR"), str(reply)[:200])
        finally:
            await ctx.close()
            bridge.terminate()

    print(f"\n{len(failures)} failure(s)" if failures else "\nall e2e checks passed")
    sys.exit(1 if failures else 0)


asyncio.run(main())
