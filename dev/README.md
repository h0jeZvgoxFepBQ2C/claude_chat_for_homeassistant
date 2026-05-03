# Dev-mode Home Assistant

Local Home Assistant in Docker for testing the Claude Chat integration end-to-end.

## First-run

```bash
make ha-up                       # starts HA on http://localhost:8123
```

HA takes ~1–2 min to start the first time. Watch logs with `make ha-logs`.

Then in a browser:

1. Visit **http://localhost:8123**.
2. Complete onboarding once: create a user (e.g. `dev` / `dev`). Skip the location/units prompts (or fill in whatever).
3. After onboarding, **trusted_networks** kicks in: every subsequent visit auto-logs you in as that user. No password screen.
4. Go to **Settings → Devices & Services → Add Integration** → search "Claude Chat" → paste your Anthropic API key.
5. Click **Claude Chat** in the sidebar.

## Day-to-day

| What | How |
|---|---|
| Restart HA after Python changes | `make ha-restart` |
| Frontend JS changes (hot reload) | hard-refresh the panel tab (`⌘⇧R`) — `cache_headers: false` is set |
| Tail logs | `make ha-logs` |
| Stop HA | `make ha-down` |
| Wipe all dev state | `make ha-clean` (deletes `dev/config/` except `configuration.yaml`) |
| Shell inside container | `make ha-shell` |

## What's pre-seeded

`configuration.yaml` defines a few template sensors so Claude has something to look at:
- `sensor.living_room_temperature`, `sensor.bedroom_temperature`, `sensor.outside_temperature`
- `sensor.living_room_humidity`
- `binary_sensor.front_door`, `binary_sensor.motion_hall`

## Auth bypass — important note

The dev config trusts `0.0.0.0/0` and enables `allow_bypass_login`. **Never copy this configuration.yaml to a production HA.** It's safe here because the container only listens on localhost.
