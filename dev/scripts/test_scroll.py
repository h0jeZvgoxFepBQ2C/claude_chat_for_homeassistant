"""E2E check: chat scrolls to bottom when opening a long session,
after navigating away and back (panel re-created, auto-select), and
after a full reload.

Prereqs: make ha-up, onboarding done, integration configured.
Usage: .venv/bin/python dev/scripts/test_scroll.py
"""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import Page, async_playwright

HA_URL = "http://localhost:8123"
# Small viewport so even mid-size sessions overflow the message list.
VIEWPORT = {"width": 900, "height": 550}

PANEL_JS = """() => {
  const ha = document.querySelector('home-assistant');
  const main = ha?.shadowRoot?.querySelector('home-assistant-main');
  const resolver = main?.shadowRoot?.querySelector('partial-panel-resolver');
  if (!resolver) return null;
  for (const el of resolver.querySelectorAll('*')) {
    if (el.tagName.toLowerCase() === 'claude-chat-panel') return el;
    const inner = el.shadowRoot?.querySelector('claude-chat-panel');
    if (inner) return inner;
  }
  return null;
}"""


async def login_if_needed(page: Page) -> None:
    try:
        await page.wait_for_selector("input[name='username']", timeout=2500, state="visible")
    except Exception:
        return
    await page.fill("input[name='username']", "dev")
    await page.fill("input[name='password']", "dev")
    await page.press("input[name='password']", "Enter")
    await page.wait_for_timeout(2000)


async def wait_panel(page: Page) -> None:
    await page.wait_for_function(
        f"() => {{ const p = ({PANEL_JS})(); return !!p && !!p.shadowRoot?.querySelector('.composer'); }}",
        timeout=20000,
    )
    await page.wait_for_timeout(1000)


async def scroll_state(page: Page) -> dict:
    return await page.evaluate(
        f"""() => {{
          const p = ({PANEL_JS})();
          const m = p.shadowRoot.querySelector('.messages');
          return {{
            title: p._activeSession?.title,
            msgs: p._activeSession?.messages?.length,
            scrollTop: m.scrollTop,
            scrollHeight: m.scrollHeight,
            clientHeight: m.clientHeight,
            scrollable: m.scrollHeight > m.clientHeight + 10,
            atBottom: m.scrollHeight - m.scrollTop - m.clientHeight < 30,
          }};
        }}"""
    )


def report(name: str, state: dict) -> bool:
    ok = state["scrollable"] and state["atBottom"]
    print(f"{'✓' if ok else '✗'} {name}: {state}")
    return ok


async def main() -> None:
    failures = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport=VIEWPORT)
        await page.goto(f"{HA_URL}/claude-chat", wait_until="domcontentloaded")
        await login_if_needed(page)
        await wait_panel(page)

        # 1. Open a long session (like clicking it in the sidebar) and bump it
        #    to the top of the list (rename touches updated_at) so the later
        #    auto-select steps pick it — the realistic re-entry scenario.
        ok = await page.evaluate(
            f"""async () => {{
              const p = ({PANEL_JS})();
              const target = p._sessions.find(s => (s.message_count ?? 1) !== 0 && /front door/i.test(s.title));
              if (!target) return false;
              await p._selectSession(target.id);
              await p._send('claude_chat/rename_session', {{
                session_id: target.id, title: p._activeSession.title,
              }});
              return true;
            }}"""
        )
        if not ok:
            print("✗ no long test session found")
            sys.exit(2)
        await page.wait_for_timeout(800)
        if not report("open long session", await scroll_state(page)):
            failures += 1

        # 2. Navigate to another HA panel and back — the panel element is
        #    re-created and auto-selects the most recent session.
        for path in ("/config/dashboard", "/claude-chat"):
            await page.evaluate(
                """(p) => {
                  history.pushState(null, '', p);
                  window.dispatchEvent(new CustomEvent('location-changed'));
                }""",
                path,
            )
            await page.wait_for_timeout(1500)
        await wait_panel(page)
        if not report("navigate away and back (auto-select)", await scroll_state(page)):
            failures += 1

        # 3. Full page reload straight into the panel.
        await page.goto(f"{HA_URL}/claude-chat", wait_until="domcontentloaded")
        await wait_panel(page)
        await page.wait_for_timeout(800)
        if not report("after full reload (auto-select)", await scroll_state(page)):
            failures += 1

        await browser.close()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
