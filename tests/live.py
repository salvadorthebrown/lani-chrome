#!/usr/bin/env python3
"""Live test: a REAL model drives the extension through the claude-local router (Anthropic Messages format).

  python3 tests/live.py MODEL [text|tools]      e.g.  python3 tests/live.py fastlane text

Loads the extension in Chromium, opens a small test page (with a cross-origin iframe), and asks the model to
fill in a name, press a button and report what the page says. Prints each step and whether the page changed.
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
CHROME = os.environ.get("LANI_CHROME") or str(
    pathlib.Path.home() / "Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64"
    / "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
ROUTER = os.environ.get("LANI_ROUTER", "http://127.0.0.1:4099")
PAGE_PORT, FRAME_PORT = 9201, 9202
TASK = ("Type the name Lani into the Name box, click the Greet button, then click the Frame button, "
        "and tell me exactly what the page says in the greeting line.")

PAGE = f"""<!doctype html><title>Lani Test</title>
<h1>Greeting machine</h1>
<label for=name>Name</label> <input id=name>
<button onclick="document.getElementById('out').textContent='Hello '+document.getElementById('name').value">Greet</button>
<p id=out>nothing yet</p><p id=out2>no frame click</p>
<iframe src="http://localhost:{FRAME_PORT}/frame.html" width=300 height=80></iframe>
<script>addEventListener('message', e => {{ document.getElementById('out2').textContent = e.data }})</script>"""
FRAME = """<!doctype html><title>frame</title>
<button onclick="parent.postMessage('frame clicked', '*')">Frame button</button>"""


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


async def main(model, mode):
    serve_html(PAGE_PORT, PAGE)
    serve_html(FRAME_PORT, FRAME)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="lani-live-"), executable_path=CHROME, headless=False,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--headless=new", "--no-first-run"])
        try:
            worker = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker", timeout=15000)
            ext_id = worker.url.split("/")[2]
            page = await ctx.new_page()
            await page.goto(f"http://127.0.0.1:{PAGE_PORT}/")
            panel = await ctx.new_page()
            await panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            await panel.evaluate("o => chrome.storage.local.set(o)", {
                "api": "anthropic", "baseUrl": ROUTER, "model": model, "mode": mode,
                "maxSteps": 12, "maxTokens": 400, "temperature": 0.2})
            await panel.reload()
            await panel.fill("#task", TASK)
            await panel.press("#task", "Enter")
            await panel.wait_for_selector(".bubble.final, .bubble.error", timeout=240000)

            print(f"\n== {model} / {mode} ==")
            for step in await panel.locator(".step").all():
                print("  step:", (await step.inner_text()).replace("\n", "  ->  ")[:160])
            print("  final:", (await panel.inner_text(".bubble.final, .bubble.error")).replace("\n", " ")[:300])
            out, out2 = await page.inner_text("#out"), await page.inner_text("#out2")
            print(f"  page state: greeting={out!r} frame={out2!r}")
            ok = out == "Hello Lani" and out2 == "frame clicked"
            print("  RESULT:", "task completed on the page" if ok else "page not fully updated")
            sys.exit(0 if ok else 1)
        finally:
            await ctx.close()


asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "text"))
