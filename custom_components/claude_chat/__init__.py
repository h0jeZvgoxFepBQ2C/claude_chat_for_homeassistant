"""Claude Chat integration for Home Assistant."""
from __future__ import annotations

import logging
import os

from homeassistant.components.frontend import (
    async_register_built_in_panel,
    async_remove_panel,
)
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .claude_client import ClaudeClient
from .const import (
    CONF_API_KEY,
    CONF_MODEL,
    DEFAULT_MODEL,
    DOMAIN,
    FRONTEND_SCRIPT,
    FRONTEND_URL,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
)
from .storage import SessionStore
from .tools import ToolRegistry
from .websocket_api import async_register_commands

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Claude Chat from a config entry."""
    api_key = entry.data[CONF_API_KEY]
    model = entry.options.get(CONF_MODEL, DEFAULT_MODEL)

    store = SessionStore(hass)
    await store.async_load()
    tools = ToolRegistry(hass, store)
    # Anthropic SDK loads SSL certs at construction time — must run in executor.
    client = await hass.async_add_executor_job(
        lambda: ClaudeClient(api_key=api_key, model=model, tools=tools)
    )

    hass.data[DOMAIN] = {
        "store": store,
        "tools": tools,
        "client": client,
        "entry": entry,
    }

    # WebSocket commands are global; register once.
    if not hass.data.get(f"{DOMAIN}_ws_registered"):
        async_register_commands(hass)
        hass.data[f"{DOMAIN}_ws_registered"] = True

    # Static frontend assets
    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, frontend_dir, cache_headers=False)]
    )

    # Sidebar panel
    async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": "claude-chat-panel",
                "embed_iframe": False,
                "trust_external": False,
                "module_url": f"{FRONTEND_URL}/{FRONTEND_SCRIPT}",
            }
        },
        require_admin=True,
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration on options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    async_remove_panel(hass, PANEL_URL_PATH)
    hass.data.pop(DOMAIN, None)
    return True
