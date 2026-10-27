#!/usr/bin/env python3
"""Loads the extension in Chromium and screenshots the side panel (empty and with settings open)."""
import asyncio, os, pathlib, sys, tempfile
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
CHROME = os.environ.get("LANI_CHROME") or str(
    pathlib.Path.home() / "Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64"
    / "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.gettempdir())


async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="lani-shot-"), executable_path=CHROME, headless=False,
            viewport={"width": 380, "height": 640},
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--headless=new", "--no-first-run"])
        worker = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker", timeout=15000)
        ext_id = worker.url.split("/")[2]
        for scheme in ("light", "dark"):
            page = await ctx.new_page()
            await page.emulate_media(color_scheme=scheme)
            await page.set_viewport_size({"width": 380, "height": 640})
            await page.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            await page.screenshot(path=str(OUT / f"panel-{scheme}.png"))
            if scheme == "light":
                await page.click("#gear")
                await page.screenshot(path=str(OUT / "panel-settings.png"))
            await page.close()
        await ctx.close()

asyncio.run(main())
