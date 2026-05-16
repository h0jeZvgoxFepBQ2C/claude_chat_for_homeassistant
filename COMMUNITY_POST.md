# Claude Chat for Home Assistant — chat panel + dashboard editing with diff approval

Hey everyone 👋

I've been hacking on a custom integration that adds a **Claude Chat** entry to the HA sidebar. The idea: a real chat panel (multiple sessions, like ChatGPT) where I can say things like *"add a card to my test dashboard with the living room temperature as a gauge"* and Claude generates the change, shows me a unified diff, and only applies it after I click **Apply**.

It's different from HA's built-in `anthropic` integration, which is a voice/Assist agent. This one is a **dashboard authoring co-pilot** — its tools are wired into Lovelace storage and HA service calls, with a hard "diff + approve" gate so nothing changes without a click.

Repo: **https://github.com/h0jeZvgoxFepBQ2C/claude_chat_for_homeassistant**

## What it does

- Sidebar panel with persistent, auto-titled chat sessions
- Streaming responses (markdown, code blocks, tables render live)
- Model picker per session (Haiku / Sonnet / Opus), choice is remembered
- Tools Claude can call:
  - `list_entities`, `get_entity`, `list_areas`
  - `list_dashboards`, `get_dashboard`, `list_lovelace_resources`
  - `list_automations`, `get_automation`, `list_services`
  - **Debugging**: `list_automation_traces`, `get_automation_trace`, `get_state_history` — ask *"why didn't my X automation fire yesterday"* and Claude reads the trace + explains
  - `propose_dashboard_update`, `propose_service_call`, `propose_automation_create / update / delete` — all **stage** changes for your approval
- Pending changes show a unified diff for dashboards, a payload preview for service calls, or the full YAML for automations. Apply or Reject from the chat UI. On approve, automations are appended to `automations.yaml` and `automation.reload` is called.

## Screenshots

(see the GitHub README for full-size — TL;DR: empty state with example prompts, sensor query showing a markdown table from `list_entities`, dashboard diff + Apply/Reject)

## Heads-up — first custom component

**This is my first HA custom component.** I'd really appreciate it if folks who know the HA internals could glance at the code and tell me anything I'm doing wrong, anti-patterns, places where I'm using deprecated APIs, etc. Areas I'm least sure about:

- The way I read/write Lovelace dashboards via `hass.data["lovelace"].dashboards` — is there a more correct/forward-compatible API?
- I had to wrap `AsyncAnthropic(...)` in `async_add_executor_job` because the SDK loads SSL certs synchronously and HA's strict mode flags it. Is there a cleaner pattern?
- Custom panel registration via `frontend_url_path` + `module_url` static path — feels right, but I'm not sure about long-term stability.
- For automation create/update/delete I'm appending to `automations.yaml` (atomic write via temp file + `os.replace`) and calling `automation.reload`. This requires the default `automation: !include automations.yaml` line. Storage-mode-only automations (no YAML backing) aren't editable yet — should I be using `homeassistant.components.config.automation` websocket commands instead?

## Privacy

The integration only sends what tools fetch (entity lists, dashboard configs, etc.) to Anthropic — it does not stream your full HA state. Sessions are stored locally in `.storage/claude_chat.sessions`. You bring your own Anthropic API key.

## Status

**Experimental — works, but new project and not fully tested.** Don't use on a critical instance yet. Issues / PRs / forum replies very welcome.

Cheers!
