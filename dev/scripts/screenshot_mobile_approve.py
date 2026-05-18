"""Mobile-viewport test: can you tap Apply on a pending-change card?

We inject a fake pending change into the panel's session state, render
it, then use Playwright's touch-tap (not mouse-click) to hit the Apply
button. We instrument _approveChange so we can tell whether the click
handler fired even if the underlying websocket call fails.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from screenshots import (  # type: ignore[import-not-found]
    HA_URL,
    chat_panel_handle,
    login_if_needed,
    open_panel,
    shot,
)

IPHONE = {"width": 390, "height": 844}


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

        # Make sure we're in a fresh session.
        handle = await chat_panel_handle(page)
        new_session = await page.evaluate(
            """async ({el}) => {
              const res = await el._send('claude_chat/create_session', {title: 'mobile tap test'});
              await el._selectSession(res.id);
              return res.id;
            }""",
            {"el": handle},
        )
        print(f"→ fresh session: {new_session}")

        # Inject a fake pending change locally + render. Also instrument
        # _approveChange so we can detect whether the click handler fires
        # (without depending on the real websocket call to succeed).
        await page.evaluate(
            """({el}) => {
              el._activeSession.pending_changes = [{
                id: 'fake-1',
                kind: 'dashboard_update',
                summary: 'Fake change to test the Apply tap on mobile',
                payload: {url_path: 'lovelace', new_config: {views: []}},
                diff: '--- current\\n+++ proposed\\n@@\\n+ test',
                source_tool_use_id: 'toolu_test',
              }];
              // Force a fake source_tool_use_id reference so it renders inline.
              const msgs = el._activeSession.messages;
              msgs.push({
                role: 'assistant',
                content: [{type: 'tool_use', id: 'toolu_test', name: 'propose_dashboard_update', input: {}}],
              });
              // Instrument _approveChange to record taps.
              window.__approveTaps = [];
              const orig = el._approveChange.bind(el);
              el._approveChange = async (id) => {
                window.__approveTaps.push(id);
                console.log('Apply tapped, id=' + id);
                // Don't call the real one — we just want to know the tap fired.
              };
              el._renderChat();
            }""",
            {"el": handle},
        )
        await page.wait_for_timeout(500)
        await shot(page, "12_mobile_pending_before_tap")

        # Locate the Apply button inside the panel's shadow root and tap it.
        # Playwright doesn't pierce shadow DOM in selectors, so go via JS.
        approve_rect = await page.evaluate(
            """({el}) => {
              const btn = el.shadowRoot.querySelector('.pending-card .approve');
              if (!btn) return null;
              const r = btn.getBoundingClientRect();
              return {x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height};
            }""",
            {"el": handle},
        )
        if not approve_rect:
            print("ERROR: Apply button not found", file=sys.stderr)
            return
        print(f"→ Apply button at {approve_rect}")

        # Real touch tap at the button center.
        await page.touchscreen.tap(approve_rect["x"], approve_rect["y"])
        await page.wait_for_timeout(500)

        taps = await page.evaluate("() => window.__approveTaps || []")
        print(f"\nresult: _approveChange called with: {taps}")
        if taps == ["fake-1"]:
            print("✓ touch-tap on Apply DOES fire the handler")
        else:
            print("✗ touch-tap on Apply DID NOT fire the handler — bug confirmed")

        await shot(page, "13_mobile_pending_after_tap")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
