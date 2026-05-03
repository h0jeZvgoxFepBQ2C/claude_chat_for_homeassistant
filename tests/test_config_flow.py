"""Config-flow tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from anthropic import AuthenticationError
from homeassistant.data_entry_flow import FlowResultType

from custom_components.claude_chat.const import CONF_API_KEY, CONF_MODEL, DOMAIN


async def test_config_flow_happy_path(hass):
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(id="ok")))
    )
    with patch(
        "custom_components.claude_chat.config_flow.AsyncAnthropic",
        return_value=fake_client,
    ), patch(
        "custom_components.claude_chat.claude_client.AsyncAnthropic",
        return_value=fake_client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_API_KEY: "sk-test", CONF_MODEL: "claude-haiku-4-5-20251001"},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == "sk-test"
    assert result["options"][CONF_MODEL] == "claude-haiku-4-5-20251001"


async def test_config_flow_invalid_auth(hass):
    fake_response = httpx.Response(
        401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    bad_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                side_effect=AuthenticationError(
                    message="bad key", response=fake_response, body=None
                )
            )
        )
    )
    with patch(
        "custom_components.claude_chat.config_flow.AsyncAnthropic",
        return_value=bad_client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_API_KEY: "wrong", CONF_MODEL: "claude-haiku-4-5-20251001"},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
