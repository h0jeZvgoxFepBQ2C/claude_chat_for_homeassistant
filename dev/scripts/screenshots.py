"""Drive the live HA dev container with Playwright and capture screenshots.

Prereqs:
  - `make ha-up` — HA running on localhost:8123
  - You completed onboarding once and added the Claude Chat integration
  - `.venv/bin/playwright install chromium`

Usage:
  .venv/bin/python dev/scripts/screenshots.py

Outputs PNGs to ./screenshots/.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import Page, async_playwright

OUT = Path(__file__).resolve().parents[2] / "screenshots"
OUT.mkdir(exist_ok=True)

HA_URL = "http://localhost:8123"
VIEWPORT = {"width": 1440, "height": 900}
DEV_USER = "dev"
DEV_PASS = "dev"


async def login_if_needed(page: Page) -> None:
    """Fill in dev/dev if HA shows a login form (auto-login may not fire for Playwright)."""
    try:
        await page.wait_for_selector(
            "input[name='username']", timeout=2500, state="visible"
        )
    except Exception:
        return  # already logged in
    await page.fill("input[name='username']", DEV_USER)
    await page.fill("input[name='password']", DEV_PASS)
    await page.press("input[name='password']", "Enter")
    # After auth, HA navigates away from /auth — wait for any HA UI shell.
    await page.wait_for_function(
        "() => !!document.querySelector('home-assistant') || "
        "!!document.querySelector('ha-auth-flow') === false",
        timeout=15000,
    )
    await page.wait_for_timeout(1500)


async def shot(page: Page, name: str, *, full_page: bool = False) -> None:
    path = OUT / f"{name}.png"
    await page.screenshot(path=str(path), full_page=full_page)
    print(f"  ✓ {path.name}")


async def open_panel(page: Page) -> None:
    """Open the Claude Chat sidebar entry."""
    await page.goto(f"{HA_URL}/claude-chat", wait_until="domcontentloaded")
    await login_if_needed(page)
    await page.wait_for_selector("home-assistant", state="attached")
    # Wait for our custom element to register inside the panel.
    await page.wait_for_function(
        """() => {
          const ha = document.querySelector('home-assistant');
          if (!ha) return false;
          const main = ha.shadowRoot?.querySelector('home-assistant-main');
          const panel = main?.shadowRoot
            ?.querySelector('partial-panel-resolver')
            ?.querySelector('claude-chat-panel, ha-panel-custom claude-chat-panel');
          return !!panel && !!panel.shadowRoot?.querySelector('.composer');
        }""",
        timeout=20000,
    )
    await page.wait_for_timeout(800)  # let initial WS round-trip settle


async def chat_panel_handle(page: Page):
    """Returns a Playwright element handle for the claude-chat-panel custom element."""
    return await page.evaluate_handle(
        """() => {
          const ha = document.querySelector('home-assistant');
          const main = ha.shadowRoot.querySelector('home-assistant-main');
          const resolver = main.shadowRoot.querySelector('partial-panel-resolver');
          // The panel might be inside ha-panel-custom or directly the custom element.
          const candidates = resolver.querySelectorAll('*');
          for (const el of candidates) {
            if (el.tagName.toLowerCase() === 'claude-chat-panel') return el;
            const inner = el.shadowRoot?.querySelector('claude-chat-panel');
            if (inner) return inner;
          }
          return null;
        }"""
    )


async def send_in_panel(page: Page, text: str) -> None:
    handle = await chat_panel_handle(page)
    await page.evaluate(
        """({el, text}) => {
          const ta = el.shadowRoot.querySelector('textarea');
          ta.value = text;
          ta.dispatchEvent(new Event('input'));
          el.shadowRoot.querySelector('.composer .send').click();
        }""",
        {"el": handle, "text": text},
    )


async def click_example(page: Page, index: int = 0) -> None:
    handle = await chat_panel_handle(page)
    await page.evaluate(
        """({el, idx}) => {
          const examples = el.shadowRoot.querySelectorAll('.empty-state .example');
          (examples[idx] || examples[0]).click();
        }""",
        {"el": handle, "idx": index},
    )


async def click_new_chat(page: Page) -> None:
    handle = await chat_panel_handle(page)
    await page.evaluate(
        """({el}) => el.shadowRoot.querySelector('#new-chat').click()""",
        {"el": handle},
    )


async def _wait_for_streaming_state(page: Page, want: bool, timeout_ms: int) -> None:
    await page.wait_for_function(
        f"""() => {{
          const ha = document.querySelector('home-assistant');
          const main = ha.shadowRoot.querySelector('home-assistant-main');
          const resolver = main.shadowRoot.querySelector('partial-panel-resolver');
          for (const el of resolver.querySelectorAll('*')) {{
            const inner = el.tagName.toLowerCase() === 'claude-chat-panel'
              ? el : el.shadowRoot?.querySelector('claude-chat-panel');
            if (inner) return inner._isStreaming === {str(want).lower()};
          }}
          return false;
        }}""",
        timeout=timeout_ms,
    )


async def wait_streaming_done(page: Page, timeout: float = 90.0) -> None:
    """Wait for streaming to start, then for it to finish."""
    await _wait_for_streaming_state(page, True, 10000)
    await _wait_for_streaming_state(page, False, int(timeout * 1000))


async def wait_pending_card(page: Page, timeout: float = 90.0) -> bool:
    try:
        await _wait_for_streaming_state(page, True, 10000)
    except Exception:
        return False
    try:
        await page.wait_for_function(
            """() => {
              const ha = document.querySelector('home-assistant');
              const main = ha.shadowRoot.querySelector('home-assistant-main');
              const resolver = main.shadowRoot.querySelector('partial-panel-resolver');
              for (const el of resolver.querySelectorAll('*')) {
                const inner = el.tagName.toLowerCase() === 'claude-chat-panel'
                  ? el : el.shadowRoot?.querySelector('claude-chat-panel');
                if (inner) return !!inner.shadowRoot.querySelector('.pending-card');
              }
              return false;
            }""",
            timeout=int(timeout * 1000),
        )
        return True
    except Exception:
        return False


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport=VIEWPORT, color_scheme="dark")
        page = await context.new_page()

        # 1. Open HA (logging in if necessary) — no screenshot here, that's
        # just the standard HA dashboard. We jump straight to the panel.
        print("→ landing page (no screenshot)")
        await page.goto(HA_URL, wait_until="domcontentloaded")
        await login_if_needed(page)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        # 2. Open the Claude Chat panel — empty state.
        print("→ claude chat panel (empty state)")
        await open_panel(page)
        # Make sure we're on the empty-state pane (no active session).
        # If a session is already active from previous runs, click + New.
        handle = await chat_panel_handle(page)
        await page.evaluate(
            """({el}) => {
              if (el.shadowRoot.querySelector('.empty-state')) return;
              // Force empty by clearing active session.
              el._activeSessionId = null;
              el._activeSession = null;
              el._renderChat();
            }""",
            {"el": handle},
        )
        await page.wait_for_timeout(400)
        await shot(page, "02_panel_empty")

        # 3. Click the first example chip (sensor question) and wait for response.
        print("→ ask about sensors")
        await click_example(page, 0)
        await wait_streaming_done(page, timeout=90)
        await page.wait_for_timeout(600)
        await shot(page, "03_sensor_response", full_page=False)

        # 4. New chat → propose a dashboard widget → pending change card.
        print("→ new chat: propose dashboard widget")
        await click_new_chat(page)
        await page.wait_for_timeout(400)
        await send_in_panel(
            page,
            "Add a card to the test dashboard showing the living room temperature sensor as a gauge.",
        )
        ok = await wait_pending_card(page, timeout=90)
        if ok:
            await page.wait_for_timeout(800)
            # Wait for streaming to finish so the natural-language summary lands.
            try:
                await _wait_for_streaming_state(page, False, 90000)
            except Exception:
                pass
            await page.wait_for_timeout(500)
            # Snapshot 04: full chat with summary at the bottom.
            await shot(page, "04_pending_change", full_page=False)
            # Snapshot 05: scroll the messages so the diff card is at the top
            # of the view, so the diff is visible without cropping.
            await page.evaluate(
                """({el}) => {
                  const card = el.shadowRoot.querySelector('.pending-card');
                  if (card) card.scrollIntoView({block: 'start', behavior: 'instant'});
                }""",
                {"el": await chat_panel_handle(page)},
            )
            await page.wait_for_timeout(400)
            await shot(page, "05_diff_review", full_page=False)
        else:
            print("  ! no pending card appeared (Claude may have asked a clarifying question)")
            await shot(page, "04_pending_fallback", full_page=False)

        # 5. New chat → propose creating an automation → automation YAML pending card.
        print("→ new chat: propose automation")
        await click_new_chat(page)
        await page.wait_for_timeout(400)
        await send_in_panel(
            page,
            "Create an automation that sends a notification when the front "
            "door opens. Use binary_sensor.front_door.",
        )
        ok = await wait_pending_card(page, timeout=120)
        if ok:
            try:
                await _wait_for_streaming_state(page, False, 120000)
            except Exception:
                pass
            await page.wait_for_timeout(500)
            await page.evaluate(
                """({el}) => {
                  const card = el.shadowRoot.querySelector('.pending-card');
                  if (card) card.scrollIntoView({block: 'start', behavior: 'instant'});
                }""",
                {"el": await chat_panel_handle(page)},
            )
            await page.wait_for_timeout(400)
            await shot(page, "06_automation_review", full_page=False)

            # 6. Click Apply and verify HA loaded the automation entity.
            print("→ click Apply on the automation pending card")
            await page.evaluate(
                """({el}) => el.shadowRoot.querySelector('.pending-card .approve').click()""",
                {"el": await chat_panel_handle(page)},
            )
            # Wait for HA to load the new automation entity.
            try:
                await page.wait_for_function(
                    """() => {
                      const ha = document.querySelector('home-assistant');
                      const states = ha.hass?.states || {};
                      return Object.keys(states).some(id => id.startsWith('automation.'));
                    }""",
                    timeout=15000,
                )
                print("  ✓ automation entity loaded into hass.states")
            except Exception as err:
                print(f"  ! automation did not load — {err}")
        else:
            print("  ! no automation pending card appeared")
            await shot(page, "06_automation_fallback", full_page=False)

        await browser.close()
        print(f"\nScreenshots saved to {OUT}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
