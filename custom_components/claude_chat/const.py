"""Constants for the Claude Chat integration."""
from __future__ import annotations

DOMAIN = "claude_chat"

CONF_AUTH_TYPE = "auth_type"
AUTH_TYPE_API_KEY = "api_key"
AUTH_TYPE_CLAUDE_ACCOUNT = "claude_account"
AUTH_TYPE_OAUTH_TOKEN = "oauth_token"

CONF_API_KEY = "api_key"
CONF_OAUTH_TOKEN = "oauth_token"
CONF_CLI_PATH = "cli_path"
CONF_CLAUDE_CONFIG_DIR = "claude_config_dir"
CONF_MODEL = "model"

CLI_DIR_NAME = "claude_chat_cli"

DEFAULT_MODEL = "claude-sonnet-5-5"

PANEL_URL_PATH = "claude-chat"
PANEL_TITLE = "Claude Chat"
PANEL_ICON = "mdi:message-text"

FRONTEND_URL = "/claude_chat_static"
FRONTEND_SCRIPT = "claude-chat-panel.js"

STORAGE_KEY = f"{DOMAIN}.sessions"
STORAGE_VERSION = 1

MAX_TURNS_PER_REQUEST = 16
DEFAULT_MAX_TOKENS = 8192
