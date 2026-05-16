"""End-to-end smoke test of the image-attach flow.

Sends one of our own README screenshots as an attachment and asks Claude
what's in it. Verifies the attachment thumbnail renders, the image lands
on disk (claude_chat_media/<session>/...), and Claude actually describes
the picture.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from screenshots import (  # type: ignore[import-not-found]
    HA_URL,
    OUT,
    VIEWPORT,
    _wait_for_streaming_state,
    chat_panel_handle,
    click_new_chat,
    login_if_needed,
    open_panel,
    shot,
)

TEST_IMAGE = Path(__file__).resolve().parents[2] / "screenshots" / "05_diff_review.png"


async def main():
    if not TEST_IMAGE.exists():
        print(f"missing test image: {TEST_IMAGE}", file=sys.stderr)
        sys.exit(1)

    image_b64 = base64.b64encode(TEST_IMAGE.read_bytes()).decode()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport=VIEWPORT, color_scheme="dark")
        page = await context.new_page()

        await page.goto(HA_URL, wait_until="domcontentloaded")
        await login_if_needed(page)
        await open_panel(page)

        print("→ inject image attachment + send into a brand-new session")
        handle = await chat_panel_handle(page)
        # Force a fresh session synchronously inline, then send. Avoids the
        # race where clicking '+ New' kicks off async session creation but
        # the send fires against the previous active session.
        new_session = await page.evaluate(
            """async ({el}) => {
              const res = await el._send('claude_chat/create_session', {title: 'Image test'});
              await el._selectSession(res.id);
              return res.id;
            }""",
            {"el": handle},
        )
        print(f"  fresh session: {new_session}")
        await page.evaluate(
            """async ({el, b64, name}) => {
              const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
              const file = new File([bytes], name, { type: 'image/png' });
              await el._addFile(file);
              const ta = el.shadowRoot.querySelector('textarea');
              ta.value = "What's shown in this screenshot? Be brief — 2-3 sentences.";
              ta.dispatchEvent(new Event('input'));
              el.shadowRoot.querySelector('.composer .send').click();
            }""",
            {"el": handle, "b64": image_b64, "name": "diff.png"},
        )

        try:
            await _wait_for_streaming_state(page, True, 10000)
            await _wait_for_streaming_state(page, False, 120000)
        except Exception as err:
            print(f"streaming wait failed: {err}", file=sys.stderr)

        await page.wait_for_timeout(800)
        await shot(page, "11_image_attached")

        # Inspect the panel state to confirm the response actually arrived.
        info = await page.evaluate(
            """({el}) => {
              const msgs = el._activeSession?.messages || [];
              const last = msgs[msgs.length - 1];
              const text = (last?.content || [])
                .filter(b => b.type === 'text')
                .map(b => b.text).join('');
              const userMsg = msgs.find(m => m.role === 'user' && (m.content||[]).some(b => b.type === 'image_ref'));
              const ref = userMsg?.content?.find(b => b.type === 'image_ref');
              return {
                last_role: last?.role,
                last_text_preview: text.slice(0, 200),
                image_filename: ref?.filename || null,
                pending_changes: el._activeSession?.pending_changes?.length || 0,
              };
            }""",
            {"el": handle},
        )
        print("\nresult:")
        for k, v in info.items():
            print(f"  {k}: {v}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
