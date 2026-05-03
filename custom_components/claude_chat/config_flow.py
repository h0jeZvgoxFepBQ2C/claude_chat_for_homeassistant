"""Config flow for Claude Chat."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from anthropic import AsyncAnthropic, AuthenticationError
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import CONF_API_KEY, CONF_MODEL, DEFAULT_MODEL, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
        vol.Optional(CONF_MODEL, default=DEFAULT_MODEL): str,
    }
)


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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate_api_key(user_input[CONF_API_KEY])
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title="Claude Chat",
                    data={CONF_API_KEY: user_input[CONF_API_KEY]},
                    options={
                        CONF_MODEL: user_input.get(CONF_MODEL, DEFAULT_MODEL)
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return ClaudeChatOptionsFlow(config_entry)


class ClaudeChatOptionsFlow(OptionsFlow):
    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_model = self.config_entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional(CONF_MODEL, default=current_model): str}
            ),
        )
