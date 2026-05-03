"""Standalone: screenshot the panel showing the missing-include warning banner.

Temporarily comments out the `automation: !include` line, restarts HA,
takes a screenshot, then restores the file.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

from screenshots import (  # type: ignore[import-not-found]
    HA_URL,
    OUT,
    VIEWPORT,
    chat_panel_handle,
    login_if_needed,
    open_panel,
    shot,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "configuration.yaml"


def restart_ha():
    subprocess.run(
        ["docker", "compose", "restart", "homeassistant"],
        cwd=str(Path(__file__).resolve().parents[2]),
        check=True,
        capture_output=True,
    )
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{HA_URL}/manifest.json", timeout=2)
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError("HA did not come back up")


async def main():
    original = CONFIG.read_text()
    if "automation: !include automations.yaml" not in original:
        print("ERR: configuration.yaml doesn't currently have the include line")
        return

    # Comment out the include line.
    patched = re.sub(
        r"^automation: !include automations\.yaml$",
        "# automation: !include automations.yaml  # temporarily commented for screenshot",
        original,
        flags=re.M,
    )
    CONFIG.write_text(patched)
    try:
        print("→ restart HA without include line")
        restart_ha()
        time.sleep(3)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport=VIEWPORT, color_scheme="dark")
            page = await context.new_page()
            await page.goto(HA_URL, wait_until="domcontentloaded")
            await login_if_needed(page)
            await open_panel(page)
            # Force the empty state so the banner is the focus, not chat history.
            handle = await chat_panel_handle(page)
            await page.evaluate(
                """({el}) => {
                  el._activeSessionId = null;
                  el._activeSession = null;
                  el._renderChat();
                }""",
                {"el": handle},
            )
            await page.wait_for_timeout(500)
            await shot(page, "07_missing_include_banner")
            await browser.close()
    finally:
        CONFIG.write_text(original)
        print("→ restore configuration.yaml + restart HA")
        restart_ha()
        print(f"  ✓ banner screenshot saved to {OUT}/07_missing_include_banner.png")


if __name__ == "__main__":
    asyncio.run(main())
