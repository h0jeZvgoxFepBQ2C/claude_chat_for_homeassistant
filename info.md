# Claude Chat for Home Assistant

A sidebar chat panel powered by Anthropic's Claude that can inspect your Home Assistant and modify your Lovelace dashboards.

## Features

- **Sidebar panel** — "Claude Chat" appears in the HA sidebar, with a chat UI and persistent sessions.
- **Tool use** — Claude can list entities/areas and read your Lovelace dashboards.
- **Diff + approve** — when Claude proposes a dashboard change, you see a diff and click Apply or Reject. Nothing changes without your OK.
- **Streaming** — responses stream live over Home Assistant's websocket.

## Setup

1. Install via HACS.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "Claude Chat".
4. Paste your Anthropic API key.
