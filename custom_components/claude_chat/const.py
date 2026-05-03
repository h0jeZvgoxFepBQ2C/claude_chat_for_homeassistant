"""Constants for the Claude Chat integration."""
from __future__ import annotations

DOMAIN = "claude_chat"

CONF_API_KEY = "api_key"
CONF_MODEL = "model"

DEFAULT_MODEL = "claude-sonnet-4-6"

PANEL_URL_PATH = "claude-chat"
PANEL_TITLE = "Claude Chat"
PANEL_ICON = "mdi:message-text"

FRONTEND_URL = "/claude_chat_static"
FRONTEND_SCRIPT = "claude-chat-panel.js"

STORAGE_KEY = f"{DOMAIN}.sessions"
STORAGE_VERSION = 1

MAX_TURNS_PER_REQUEST = 16
DEFAULT_MAX_TOKENS = 4096
