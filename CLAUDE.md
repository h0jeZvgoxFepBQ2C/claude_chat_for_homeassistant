# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (`custom_components/claude_chat/`) that adds a sidebar chat panel powered by Anthropic's API. The Python backend exposes scoped HA tools to Claude; a vanilla-JS custom element renders the chat UI and streams responses over HA's websocket. Distributed via HACS — see `manifest.json` for the version.

## Common commands

All driven by the Makefile (creates `.venv` on demand):

```bash
make setup            # create venv + install requirements-test.txt
make test             # run pytest (uses pytest-homeassistant-custom-component + FakeAsyncAnthropic)
make lint             # ruff check custom_components tests
make format           # ruff format custom_components tests

make ha-up            # docker compose up — local HA at http://localhost:8123
make ha-restart       # restart HA after Python integration changes (frontend JS hot-reloads)
make ha-logs          # tail HA container logs
make ha-clean         # wipe dev/config (keeps configuration.yaml) for a fresh slate
```

Run a single test: `.venv/bin/pytest tests/test_tools.py::test_propose_dashboard_supersedes_older -q`

Frontend JS changes: hard-refresh the panel tab (`⌘⇧R`). No bundler. The integration computes a SHA-256 prefix of `claude-chat-panel.js` at setup and appends it as `?v=<hash>` to the panel module URL — when you restart HA the URL changes and browsers/the HA companion WebView fetch the new file even through aggressive caches.

End-to-end screenshots via Playwright: `.venv/bin/python dev/scripts/screenshots.py` (requires `make ha-up` running and the integration configured).

## Architecture

### Boot flow (`__init__.py`)
`async_setup_entry` wires up the four singletons and stores them in `hass.data[DOMAIN]`:
- `SessionStore` — HA `Store` helper persisting `Session`/`Message`/`PendingChange` to `.storage/claude_chat.sessions`.
- `ToolRegistry` — dispatch table for Claude's tool calls; holds a reference to the store so `propose_*` can stage `PendingChange`s.
- `ClaudeClient` — Anthropic SDK wrapper. Built in an executor because the SDK loads SSL certs at construction time (would block the event loop otherwise).
- Built-in panel registered with the cache-busted `module_url`.

Websocket commands are global to the HA instance, registered exactly once (guarded by a `_ws_registered` flag) so reloading the entry doesn't re-register and crash.

### Request lifecycle
1. Frontend's `_sendMessage` calls `connection.subscribeMessage` against `claude_chat/send_message`. This is a long-lived subscription, not a one-shot.
2. `ws_send_message` saves the user message, then calls `ClaudeClient.stream_chat`, passing an `emit` callback that forwards each event back to the subscription.
3. `stream_chat` runs the tool-use loop (up to `MAX_TURNS_PER_REQUEST=16`): for each turn it streams the Anthropic API response, emits `text_delta` / `tool_use_start` / `tool_result` events to the frontend, executes any tools, and appends the assistant + tool_result messages to history. Loop exits when `stop_reason != "tool_use"`.
4. After the loop, the first user message in a session triggers a Haiku-powered `summarize_title` to rename the session.
5. A final `done` event ships the fully-refreshed session back so the UI re-renders from authoritative state.

### Session-state block (system prompt, per-turn)
`_session_state_block` builds a `[Session state — status of past proposals in this chat]` summary from `session.pending_changes` (✓ accepted / ✗ rejected / ⏳ pending) and appends it to the system prompt on every API call. The prompt explicitly labels it INFORMATIONAL because past iterations of Claude misread it as "everything's handled" and skipped calling `propose_*` for new user requests — a known failure mode. If you're touching the prompt, preserve the explicit rule: *"If the user asks for a new change, you MUST still call the corresponding propose_* tool — describing the change in text without staging it is a bug."*

### Tools (`tools.py`)
- **Read-only**: `list_entities`, `get_entity`, `list_areas`, `list_dashboards`, `get_dashboard`, `list_lovelace_resources`, `list_automations`, `get_automation`, `get_script`, `get_helper`, `list_services`, `list_automation_traces`, `get_automation_trace`, `get_state_history`.
- **Staged**: `propose_dashboard_update`, `propose_service_call`, `propose_automation_*`, `propose_script_*`, `propose_helper_*` (create/update/delete each). These never mutate HA; they create a `PendingChange` (with `source_tool_use_id` so the frontend can render the card inline next to the tool chip that produced it) and return `awaiting_user_approval`.
- Each `propose_*` runs through `_add_pending_supersede`: any *still-pending* change with the same `_target_key` is dropped before adding the new one. Accepted/rejected changes are kept as history. **`_target_key` (Python) and `targetKeyFor` (frontend JS, ~line 1295) must stay in sync** — they define what counts as "the same target" (same dashboard url_path, same automation_id, same `domain.service` + target dict, etc.).
- `apply_pending_change` runs from `ws_approve_change` only. For automations it edits `automations.yaml` atomically (tmp + `os.replace`), calls `automation.reload`, then *verifies* the automation actually loaded — if not, it surfaces the "your `configuration.yaml` is missing `automation: !include automations.yaml`" error. Scripts work the same way against `scripts.yaml` (a dict keyed by script_id, unlike the automations list) with `script: !include scripts.yaml`.
- Input helpers (`input_boolean`, `input_number`, `input_text`, `input_select`, `input_datetime`, `input_button`) are created/updated/deleted through the domains' storage collections — the same path the HA Helpers UI uses, so changes apply live without a reload. The collection instance is a local variable in each component's `async_setup`, so `_helper_collection` recovers it by unwrapping the registered `<domain>/create` websocket handler (`functools.wraps` chain → bound method → `StorageCollectionWebsocket.storage_collection`). Only storage-backed (UI-created) helpers are editable; YAML-defined ones are rejected with a clear error.

### Storage shape
`Message.content` mirrors Anthropic's API content-block shape (`{type: "text"|"tool_use"|"tool_result"|"image"|"image_ref", ...}`). The custom `image_ref` block points at a file under `<config>/claude_chat_media/<session_id>/<uuid>.<ext>`; `media.py::to_api_content` inlines those as base64 `image` blocks just before the API call so disk-stored attachments never bloat the persisted JSON.

`PendingChange.status` is `pending` → `accepted`/`rejected` (set on apply/reject), never deleted. Older pending entries for the same target get *removed* by supersede; resolved ones stay so the chat transcript and the system-prompt session-state block remain coherent.

### Frontend (`frontend/claude-chat-panel.js`, ~1370 lines, single file, no build)
Vanilla JS custom element registered as `claude-chat-panel`. Holds session list + active session in instance state; re-renders from `_activeSession.messages` + `_activeSession.pending_changes` on every change. The renderer matches `PendingChange.source_tool_use_id` against assistant `tool_use` blocks so each pending card appears directly under its originating tool call — orphans (no match) fall through to a bottom-of-thread fallback.

Markdown is rendered via the bundled `marked.min.js`. Composer drafts are persisted to `localStorage` keyed by session id.

## Testing notes

- `tests/fake_anthropic.py` provides `FakeAsyncAnthropic` — script it with `text_event(...)` and `tool_use_event(...)` builders in the order you expect Claude to respond. Running out of scripted responses raises (so missing scripts surface as test errors, not silent hangs).
- `conftest.py` stubs out `hass_frontend` (the built JS bundle isn't installed in test envs) and provides a `configured_entry` fixture that sets up the integration with the fake API client.
- Tests assert on `fake_anthropic.calls` (kwargs of each `messages.stream` call) when you need to verify what was sent to the API — e.g. that the system prompt contained the right session-state block.

## Dev container caveats

`dev/config/configuration.yaml` enables `trusted_networks` for `0.0.0.0/0` and `allow_bypass_login: true` so you don't see a login screen. **Never copy this file to a real HA instance.** It only listens on localhost.
