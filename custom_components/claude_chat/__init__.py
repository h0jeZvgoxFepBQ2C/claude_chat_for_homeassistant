"""Claude Chat integration for Home Assistant."""
from __future__ import annotations

import hashlib
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
from .media import ensure_media_root, media_root
from .storage import SessionStore
from .tools import ToolRegistry
from .websocket_api import async_register_commands

MEDIA_URL = "/claude_chat_media"

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

    # Static frontend assets + user-uploaded image storage.
    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    panel_js_path = os.path.join(frontend_dir, FRONTEND_SCRIPT)
    asset_hash = await hass.async_add_executor_job(_asset_hash, panel_js_path)
    await hass.async_add_executor_job(ensure_media_root, hass)
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(FRONTEND_URL, frontend_dir, cache_headers=False),
            StaticPathConfig(MEDIA_URL, media_root(hass), cache_headers=True),
        ]
    )

    # Sidebar panel. We append ?v=<sha-prefix> so that whenever the JS
    # changes, the browser / service worker / HA-companion WebView see a
    # different URL and fetch the new file — otherwise old code can stick
    # in caches even after HACS updates the file on disk.
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
                "module_url": f"{FRONTEND_URL}/{FRONTEND_SCRIPT}?v={asset_hash}",
            }
        },
        require_admin=True,
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration on options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _asset_hash(path: str) -> str:
    """SHA-256 prefix of a file's bytes — used as a cache-busting URL param."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return "0"
    return h.hexdigest()[:10]


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    async_remove_panel(hass, PANEL_URL_PATH)
    hass.data.pop(DOMAIN, None)
    return True
