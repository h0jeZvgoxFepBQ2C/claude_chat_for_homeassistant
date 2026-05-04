"""Capture mobile-viewport screenshots of the panel."""
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from screenshots import HA_URL, OUT, login_if_needed, open_panel, chat_panel_handle, shot

IPHONE = {"width": 390, "height": 844}  # iPhone 14


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport=IPHONE,
            color_scheme="dark",
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
        )
        page = await context.new_page()

        await page.goto(HA_URL, wait_until="domcontentloaded")
        await login_if_needed(page)
        await open_panel(page)
        # Force empty state for cleaner shot
        handle = await chat_panel_handle(page)
        await page.evaluate(
            """({el}) => {
              el._activeSessionId = null;
              el._activeSession = null;
              el._renderChat();
              el.removeAttribute('data-sidebar-open');
            }""",
            {"el": handle},
        )
        await page.wait_for_timeout(500)
        await shot(page, "08_mobile_empty")

        # Open the sidebar drawer.
        await page.evaluate(
            """({el}) => el.shadowRoot.querySelector('.menu-toggle').click()""",
            {"el": handle},
        )
        await page.wait_for_timeout(400)
        await shot(page, "09_mobile_sidebar")

        # Close + send a message
        await page.evaluate(
            """({el}) => el.shadowRoot.querySelector('.sidebar-backdrop').click()""",
            {"el": handle},
        )
        await page.wait_for_timeout(300)
        await page.evaluate(
            """({el}) => {
              const ta = el.shadowRoot.querySelector('textarea');
              ta.value = 'What sensors do I have?';
              ta.dispatchEvent(new Event('input'));
              el.shadowRoot.querySelector('.composer .send').click();
            }""",
            {"el": handle},
        )
        # wait for streaming to start, then for it to finish
        try:
            await page.wait_for_function(
                """() => {
                  const ha = document.querySelector('home-assistant');
                  const main = ha.shadowRoot.querySelector('home-assistant-main');
                  const resolver = main.shadowRoot.querySelector('partial-panel-resolver');
                  for (const el of resolver.querySelectorAll('*')) {
                    const inner = el.tagName.toLowerCase() === 'claude-chat-panel'
                      ? el : el.shadowRoot?.querySelector('claude-chat-panel');
                    if (inner) return inner._isStreaming === true;
                  }
                  return false;
                }""",
                timeout=10000,
            )
            await page.wait_for_function(
                """() => {
                  const ha = document.querySelector('home-assistant');
                  const main = ha.shadowRoot.querySelector('home-assistant-main');
                  const resolver = main.shadowRoot.querySelector('partial-panel-resolver');
                  for (const el of resolver.querySelectorAll('*')) {
                    const inner = el.tagName.toLowerCase() === 'claude-chat-panel'
                      ? el : el.shadowRoot?.querySelector('claude-chat-panel');
                    if (inner) return inner._isStreaming === false;
                  }
                  return false;
                }""",
                timeout=90000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(800)
        await shot(page, "10_mobile_chat")

        await browser.close()
        print(f"\nMobile screenshots saved to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
