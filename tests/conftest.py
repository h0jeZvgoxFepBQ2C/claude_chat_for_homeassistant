"""Shared pytest fixtures for claude_chat tests."""
from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

# `homeassistant.components.frontend` requires the `hass_frontend` package
# (the built JS bundle), which isn't installed in test envs. Stub it so the
# frontend component sets up cleanly during integration loading.
if "hass_frontend" not in sys.modules:
    stub = ModuleType("hass_frontend")
    stub.where = lambda: Path("/tmp")  # noqa: E731
    sys.modules["hass_frontend"] = stub

import pytest  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.claude_chat.const import (  # noqa: E402
    CONF_API_KEY,
    CONF_MODEL,
    DOMAIN,
)

from .fake_anthropic import FakeAsyncAnthropic  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ARG001
    """Required by pytest-homeassistant-custom-component for custom integrations."""
    yield


@pytest.fixture
def fake_anthropic() -> Generator[FakeAsyncAnthropic, None, None]:
    """Patch the AsyncAnthropic class used by claude_client to a scripted fake."""
    fake = FakeAsyncAnthropic()
    with patch(
        "custom_components.claude_chat.claude_client.AsyncAnthropic",
        return_value=fake,
    ), patch(
        "custom_components.claude_chat.config_flow.AsyncAnthropic",
        return_value=SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **kwargs: _ok_create(),
            )
        ),
    ):
        yield fake


async def _ok_create():
    """Minimal stub for the config-flow auth probe."""
    return SimpleNamespace(id="ok")


@pytest.fixture
async def configured_entry(hass, fake_anthropic):  # noqa: ARG001
    """A configured & loaded claude_chat entry, ready for tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "sk-test"},
        options={CONF_MODEL: "claude-haiku-4-5-20251001"},
        title="Claude Chat",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
