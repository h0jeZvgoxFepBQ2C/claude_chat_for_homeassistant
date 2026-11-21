"""Config flow for Claude Chat."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from anthropic import AsyncAnthropic, AuthenticationError
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .claude_cli import (
    ClaudeCliAuthError,
    ClaudeCliDownloadError,
    ClaudeLoginSession,
    async_check_auth_status,
    async_ensure_cli_binary,
    async_logout,
    async_validate_cli_auth,
    default_claude_config_dir,
)
from .const import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_CLAUDE_ACCOUNT,
    AUTH_TYPE_OAUTH_TOKEN,
    CONF_API_KEY,
    CONF_AUTH_TYPE,
    CONF_CLAUDE_CONFIG_DIR,
    CONF_CLI_PATH,
    CONF_MODEL,
    CONF_OAUTH_TOKEN,
    DEFAULT_MODEL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_AUTH_CODE = "auth_code"
CONF_FRESH_LOGIN = "fresh_login"


async def _validate_api_key(api_key: str) -> None:
    client = AsyncAnthropic(api_key=api_key)
    # Cheapest possible call to verify auth.
    await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "hi"}],
    )


class ClaudeChatConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._cli_path: str | None = None
        self._config_dir: str | None = None
        self._auth_url: str | None = None
        self._account_label: str = "Claude account"
        self._login_session: ClaudeLoginSession | None = None
        self._reconfigure_entry: ConfigEntry | None = None

    def _current_model(self) -> str:
        if self._reconfigure_entry is not None:
            return self._reconfigure_entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        return DEFAULT_MODEL

    async def _close_login_session(self) -> None:
        if self._login_session is not None:
            session = self._login_session
            self._login_session = None
            await session.close()

    @callback
    def async_remove(self) -> None:
        """Clean up any running login PTY when the flow is aborted/removed."""
        if self._login_session is not None:
            session = self._login_session
            self._login_session = None
            self.hass.async_create_task(session.close())

    def _finish_entry(
        self, *, data: dict[str, Any], model: str
    ) -> ConfigFlowResult:
        if self._reconfigure_entry is not None:
            return self.async_update_reload_and_abort(
                self._reconfigure_entry,
                data=data,
                options={**self._reconfigure_entry.options, CONF_MODEL: model},
            )
        return self.async_create_entry(
            title="Claude Chat",
            data=data,
            options={CONF_MODEL: model},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        return self.async_show_menu(
            step_id="user",
            menu_options=[
                AUTH_TYPE_CLAUDE_ACCOUNT,
                AUTH_TYPE_OAUTH_TOKEN,
                AUTH_TYPE_API_KEY,
            ],
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._reconfigure_entry = self._get_reconfigure_entry()
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                AUTH_TYPE_CLAUDE_ACCOUNT,
                AUTH_TYPE_OAUTH_TOKEN,
                AUTH_TYPE_API_KEY,
            ],
        )

    async def async_step_claude_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prepare the Claude Code CLI and launch interactive Claude account login."""
        errors: dict[str, str] = {}
        custom_cli_path = (
            user_input.get(CONF_CLI_PATH) or None if user_input is not None else None
        )
        custom_config_dir = (
            user_input.get(CONF_CLAUDE_CONFIG_DIR) or None
            if user_input is not None
            else None
        )

        config_dir = custom_config_dir or str(default_claude_config_dir(self.hass))
        try:
            cli_path = await async_ensure_cli_binary(
                self.hass, configured_path=custom_cli_path
            )
        except ClaudeCliDownloadError as err:
            _LOGGER.warning("Failed to prepare Claude CLI binary: %s", err)
            errors["base"] = "cli_download_failed"
            return self.async_show_form(
                step_id="claude_account",
                data_schema=vol.Schema(
                    {
                        vol.Optional(CONF_CLI_PATH, default=custom_cli_path or ""): str,
                        vol.Optional(
                            CONF_CLAUDE_CONFIG_DIR, default=custom_config_dir or ""
                        ): str,
                    }
                ),
                errors=errors,
            )

        self._cli_path = cli_path
        self._config_dir = config_dir

        if self._reconfigure_entry is not None:
            # Force a fresh login when reconfiguring so the user can switch or
            # refresh their Claude account.
            await async_logout(cli_path, config_dir)
        else:
            status = await async_check_auth_status(cli_path, config_dir=config_dir)
            if status.get("loggedIn"):
                self._account_label = str(
                    status.get("email")
                    or status.get("subscriptionType")
                    or status.get("authMethod")
                    or "Claude account"
                )
                return await self.async_step_claude_account_confirm()

        try:
            await self._start_interactive_login()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to start Claude account sign-in: %s", err)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="claude_account",
                data_schema=vol.Schema(
                    {
                        vol.Optional(CONF_CLI_PATH, default=custom_cli_path or ""): str,
                        vol.Optional(
                            CONF_CLAUDE_CONFIG_DIR, default=custom_config_dir or ""
                        ): str,
                    }
                ),
                errors=errors,
            )

        return await self.async_step_claude_account_code()

    async def _start_interactive_login(self) -> None:
        assert self._cli_path is not None and self._config_dir is not None
        await self._close_login_session()
        session = ClaudeLoginSession(self._cli_path, self._config_dir)
        self._auth_url = await session.start()
        self._login_session = session

    async def async_step_claude_account_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm using an already signed-in Claude account in CLAUDE_CONFIG_DIR."""
        if user_input is not None:
            if user_input.get(CONF_FRESH_LOGIN):
                assert self._cli_path is not None and self._config_dir is not None
                await async_logout(self._cli_path, self._config_dir)
                await self._start_interactive_login()
                return await self.async_step_claude_account_code()

            return self._finish_entry(
                data={
                    CONF_AUTH_TYPE: AUTH_TYPE_CLAUDE_ACCOUNT,
                    CONF_CLI_PATH: self._cli_path,
                    CONF_CLAUDE_CONFIG_DIR: self._config_dir,
                },
                model=user_input.get(CONF_MODEL, self._current_model()),
            )

        return self.async_show_form(
            step_id="claude_account_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_MODEL, default=self._current_model()): str,
                    vol.Optional(CONF_FRESH_LOGIN, default=False): bool,
                }
            ),
            description_placeholders={"account": self._account_label},
        )

    async def async_step_claude_account_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the OAuth sign-in link and accept the verification code."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input[CONF_AUTH_CODE].strip()
            if not code or self._login_session is None:
                errors["base"] = "invalid_auth"
            else:
                ok, detail = await self._login_session.submit_code(code)
                self._login_session = None
                if ok:
                    return self._finish_entry(
                        data={
                            CONF_AUTH_TYPE: AUTH_TYPE_CLAUDE_ACCOUNT,
                            CONF_CLI_PATH: self._cli_path,
                            CONF_CLAUDE_CONFIG_DIR: self._config_dir,
                        },
                        model=user_input.get(CONF_MODEL, self._current_model()),
                    )
                _LOGGER.warning("Claude account code rejected: %s", detail)
                errors["base"] = "invalid_auth"

            # Start a fresh login process so the user gets a valid URL to retry.
            try:
                await self._start_interactive_login()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not restart Claude sign-in: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="claude_account_code",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_CODE): str,
                    vol.Optional(CONF_MODEL, default=self._current_model()): str,
                }
            ),
            description_placeholders={"auth_url": self._auth_url or ""},
            errors=errors,
        )

    async def async_step_oauth_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate using a CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_OAUTH_TOKEN].strip()
            config_dir = str(default_claude_config_dir(self.hass))
            try:
                cli_path = await async_ensure_cli_binary(self.hass)
                await async_validate_cli_auth(
                    cli_path, config_dir=config_dir, oauth_token=token
                )
            except ClaudeCliDownloadError as err:
                _LOGGER.warning("Failed to prepare Claude CLI binary: %s", err)
                errors["base"] = "cli_download_failed"
            except ClaudeCliAuthError:
                errors["base"] = "invalid_auth"
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not validate Claude setup token: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return self._finish_entry(
                    data={
                        CONF_AUTH_TYPE: AUTH_TYPE_OAUTH_TOKEN,
                        CONF_OAUTH_TOKEN: token,
                        CONF_CLI_PATH: cli_path,
                        CONF_CLAUDE_CONFIG_DIR: config_dir,
                    },
                    model=user_input.get(CONF_MODEL, self._current_model()),
                )

        return self.async_show_form(
            step_id="oauth_token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OAUTH_TOKEN): str,
                    vol.Optional(CONF_MODEL, default=self._current_model()): str,
                }
            ),
            errors=errors,
        )

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate using a standard Anthropic Console API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate_api_key(user_input[CONF_API_KEY])
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return self._finish_entry(
                    data={
                        CONF_AUTH_TYPE: AUTH_TYPE_API_KEY,
                        CONF_API_KEY: user_input[CONF_API_KEY],
                    },
                    model=user_input.get(CONF_MODEL, self._current_model()),
                )

        return self.async_show_form(
            step_id="api_key",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Optional(CONF_MODEL, default=self._current_model()): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ClaudeChatOptionsFlow()


class ClaudeChatOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_model = self.config_entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        auth_type = self.config_entry.data.get(CONF_AUTH_TYPE, AUTH_TYPE_API_KEY)
        schema_fields: dict[Any, Any] = {
            vol.Optional(CONF_MODEL, default=current_model): str
        }
        if auth_type in (AUTH_TYPE_CLAUDE_ACCOUNT, AUTH_TYPE_OAUTH_TOKEN):
            schema_fields[
                vol.Optional(
                    CONF_CLI_PATH,
                    default=self.config_entry.options.get(
                        CONF_CLI_PATH, self.config_entry.data.get(CONF_CLI_PATH, "")
                    ),
                )
            ] = str
            schema_fields[
                vol.Optional(
                    CONF_CLAUDE_CONFIG_DIR,
                    default=self.config_entry.options.get(
                        CONF_CLAUDE_CONFIG_DIR,
                        self.config_entry.data.get(CONF_CLAUDE_CONFIG_DIR, ""),
                    ),
                )
            ] = str

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
        )
