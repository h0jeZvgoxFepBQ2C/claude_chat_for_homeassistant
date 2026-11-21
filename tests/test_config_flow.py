"""Config-flow tests for API key, Claude account OAuth, setup token, and reconfigure."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from anthropic import AuthenticationError
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.claude_chat.claude_cli import (
    ClaudeCliAuthError,
    ClaudeCliDownloadError,
)
from custom_components.claude_chat.const import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_CLAUDE_ACCOUNT,
    AUTH_TYPE_OAUTH_TOKEN,
    CONF_API_KEY,
    CONF_AUTH_TYPE,
    CONF_CLAUDE_CONFIG_DIR,
    CONF_CLI_PATH,
    CONF_MODEL,
    CONF_OAUTH_TOKEN,
    DOMAIN,
)


async def test_config_flow_api_key_happy_path(hass):
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
        assert result["type"] == FlowResultType.MENU

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": AUTH_TYPE_API_KEY}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "api_key"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_API_KEY: "sk-test", CONF_MODEL: "claude-haiku-4-5-20251001"},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_TYPE] == AUTH_TYPE_API_KEY
    assert result["data"][CONF_API_KEY] == "sk-test"
    assert result["options"][CONF_MODEL] == "claude-haiku-4-5-20251001"


async def test_config_flow_api_key_invalid_auth(hass):
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
            result["flow_id"], {"next_step_id": AUTH_TYPE_API_KEY}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_API_KEY: "wrong", CONF_MODEL: "claude-haiku-4-5-20251001"},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_config_flow_claude_account_interactive_login(hass):
    fake_session = SimpleNamespace(
        start=AsyncMock(
            return_value="https://claude.com/cai/oauth/authorize?code=true&state=abc"
        ),
        submit_code=AsyncMock(return_value=(True, "")),
        close=AsyncMock(),
    )
    with patch(
        "custom_components.claude_chat.config_flow.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.config_flow.async_check_auth_status",
        new=AsyncMock(return_value={"loggedIn": False, "authMethod": "none"}),
    ), patch(
        "custom_components.claude_chat.config_flow.ClaudeLoginSession",
        return_value=fake_session,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.MENU

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": AUTH_TYPE_CLAUDE_ACCOUNT}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "claude_account_code"
        assert (
            result["description_placeholders"]["auth_url"]
            == "https://claude.com/cai/oauth/authorize?code=true&state=abc"
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"auth_code": "code123#state", CONF_MODEL: "claude-sonnet-5"},
        )
        await hass.async_block_till_done()

    fake_session.submit_code.assert_awaited_once_with("code123#state")
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_TYPE] == AUTH_TYPE_CLAUDE_ACCOUNT
    assert result["data"][CONF_CLI_PATH] == "/usr/local/bin/claude"
    assert result["data"][CONF_CLAUDE_CONFIG_DIR]
    assert result["options"][CONF_MODEL] == "claude-sonnet-5"


async def test_config_flow_claude_account_already_logged_in(hass):
    with patch(
        "custom_components.claude_chat.config_flow.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.config_flow.async_check_auth_status",
        new=AsyncMock(
            return_value={"loggedIn": True, "email": "alice@example.com"}
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": AUTH_TYPE_CLAUDE_ACCOUNT}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "claude_account_confirm"
        assert result["description_placeholders"]["account"] == "alice@example.com"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_MODEL: "claude-opus-5", "fresh_login": False},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_TYPE] == AUTH_TYPE_CLAUDE_ACCOUNT
    assert result["options"][CONF_MODEL] == "claude-opus-5"


async def test_config_flow_claude_account_invalid_code_retries(hass):
    session_1 = SimpleNamespace(
        start=AsyncMock(return_value="https://claude.com/cai/oauth/authorize?s=1"),
        submit_code=AsyncMock(return_value=(False, "Invalid code")),
        close=AsyncMock(),
    )
    session_2 = SimpleNamespace(
        start=AsyncMock(return_value="https://claude.com/cai/oauth/authorize?s=2"),
        submit_code=AsyncMock(return_value=(True, "")),
        close=AsyncMock(),
    )
    sessions = [session_1, session_2]

    with patch(
        "custom_components.claude_chat.config_flow.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.config_flow.async_check_auth_status",
        new=AsyncMock(return_value={"loggedIn": False}),
    ), patch(
        "custom_components.claude_chat.config_flow.ClaudeLoginSession",
        side_effect=lambda *a, **kw: sessions.pop(0),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": AUTH_TYPE_CLAUDE_ACCOUNT}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"auth_code": "bad-code", CONF_MODEL: "claude-sonnet-5"}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}
        assert (
            result["description_placeholders"]["auth_url"]
            == "https://claude.com/cai/oauth/authorize?s=2"
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"auth_code": "good-code", CONF_MODEL: "claude-sonnet-5"}
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_config_flow_cli_download_error_shows_custom_path_form(hass):
    with patch(
        "custom_components.claude_chat.config_flow.async_ensure_cli_binary",
        new=AsyncMock(side_effect=ClaudeCliDownloadError("offline")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": AUTH_TYPE_CLAUDE_ACCOUNT}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "claude_account"
    assert result["errors"] == {"base": "cli_download_failed"}


async def test_config_flow_oauth_token_happy_and_invalid(hass):
    with patch(
        "custom_components.claude_chat.config_flow.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.config_flow.async_validate_cli_auth",
        new=AsyncMock(side_effect=[ClaudeCliAuthError("bad token"), None]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": AUTH_TYPE_OAUTH_TOKEN}
        )
        assert result["step_id"] == "oauth_token"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_OAUTH_TOKEN: "sk-ant-oat01-bad", CONF_MODEL: "claude-sonnet-5"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_OAUTH_TOKEN: "sk-ant-oat01-good", CONF_MODEL: "claude-sonnet-5"},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_TYPE] == AUTH_TYPE_OAUTH_TOKEN
    assert result["data"][CONF_OAUTH_TOKEN] == "sk-ant-oat01-good"


async def test_config_flow_reconfigure_api_key_to_claude_account(hass, fake_anthropic):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "sk-old"},
        options={CONF_MODEL: "claude-sonnet-5"},
        title="Claude Chat",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    fake_session = SimpleNamespace(
        start=AsyncMock(return_value="https://claude.com/cai/oauth/authorize?reconf=1"),
        submit_code=AsyncMock(return_value=(True, "")),
        close=AsyncMock(),
    )
    with patch(
        "custom_components.claude_chat.config_flow.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.config_flow.async_logout",
        new=AsyncMock(),
    ) as mock_logout, patch(
        "custom_components.claude_chat.config_flow.ClaudeLoginSession",
        return_value=fake_session,
    ):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == FlowResultType.MENU
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": AUTH_TYPE_CLAUDE_ACCOUNT}
        )
        assert result["step_id"] == "claude_account_code"
        mock_logout.assert_awaited_once()

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"auth_code": "fresh-code", CONF_MODEL: "claude-opus-5"},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_AUTH_TYPE] == AUTH_TYPE_CLAUDE_ACCOUNT
    assert CONF_API_KEY not in entry.data
    assert entry.options[CONF_MODEL] == "claude-opus-5"


async def test_config_flow_abort_closes_login_session(hass):
    fake_session = SimpleNamespace(
        start=AsyncMock(return_value="https://claude.com/cai/oauth/authorize?s=abort"),
        submit_code=AsyncMock(return_value=(True, "")),
        close=AsyncMock(),
    )
    with patch(
        "custom_components.claude_chat.config_flow.async_ensure_cli_binary",
        new=AsyncMock(return_value="/usr/local/bin/claude"),
    ), patch(
        "custom_components.claude_chat.config_flow.async_check_auth_status",
        new=AsyncMock(return_value={"loggedIn": False}),
    ), patch(
        "custom_components.claude_chat.config_flow.ClaudeLoginSession",
        return_value=fake_session,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": AUTH_TYPE_CLAUDE_ACCOUNT}
        )
        assert result["step_id"] == "claude_account_code"

        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()

    fake_session.close.assert_awaited_once()


async def test_options_flow_for_claude_account_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_AUTH_TYPE: AUTH_TYPE_CLAUDE_ACCOUNT,
            CONF_CLI_PATH: "/config/claude_chat_cli/claude",
            CONF_CLAUDE_CONFIG_DIR: "/config/claude_chat_cli/config",
        },
        options={CONF_MODEL: "claude-sonnet-5-5"},
        title="Claude Chat",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.claude_chat.async_ensure_cli_binary",
        new=AsyncMock(return_value="/config/claude_chat_cli/claude"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_MODEL: "claude-opus-5-5",
                CONF_CLI_PATH: "/config/claude_chat_cli/claude",
                CONF_CLAUDE_CONFIG_DIR: "/config/claude_chat_cli/config",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MODEL] == "claude-opus-5-5"


async def test_options_flow_for_api_key_entry(hass, fake_anthropic):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "sk-test"},
        options={CONF_MODEL: "claude-sonnet-5-5"},
        title="Claude Chat",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_MODEL: "claude-haiku-5-5"},
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MODEL] == "claude-haiku-5-5"
