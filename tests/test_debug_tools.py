"""Tests for the debugging tools: traces + state history."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.claude_chat.storage import SessionStore
from custom_components.claude_chat.tools import ToolRegistry


@pytest.fixture
async def setup(hass):
    store = SessionStore(hass)
    await store.async_load()
    session = await store.create()
    return ToolRegistry(hass, store), store, session.id


async def test_list_traces_no_data(setup, hass):
    """When no traces are stored yet, return an explanatory note, not an error."""
    tools, _, sid = setup
    result = await tools.call(
        "list_automation_traces", {"automation_id": "abc"}, sid
    )
    assert result["traces"] == []
    assert "note" in result


async def test_list_traces_with_fake_data(setup, hass):
    """Seed hass.data[DATA_TRACE] with fakes and verify they come back sorted."""
    from homeassistant.components.trace.const import DATA_TRACE

    tools, _, sid = setup

    fake_run_1 = SimpleNamespace(
        timestamp_start="2026-01-01T10:00:00",
        timestamp_finish="2026-01-01T10:00:01",
        as_short_dict=lambda: {"run_id": "r1", "state": "stopped"},
        as_dict=lambda: {"run_id": "r1", "trace": "..."},
    )
    fake_run_2 = SimpleNamespace(
        timestamp_start="2026-01-02T10:00:00",
        timestamp_finish="2026-01-02T10:00:01",
        as_short_dict=lambda: {"run_id": "r2", "state": "stopped"},
        as_dict=lambda: {"run_id": "r2", "trace": "..."},
    )
    hass.data[DATA_TRACE] = {"automation": {"abc": {"r1": fake_run_1, "r2": fake_run_2}}}

    result = await tools.call(
        "list_automation_traces", {"automation_id": "abc"}, sid
    )
    assert len(result["traces"]) == 2
    # Newer first
    assert result["traces"][0]["run_id"] == "r2"


async def test_get_trace_returns_full_detail(setup, hass):
    from homeassistant.components.trace.const import DATA_TRACE

    tools, _, sid = setup
    fake_run = SimpleNamespace(
        timestamp_start="2026-01-01T10:00:00",
        timestamp_finish="2026-01-01T10:00:01",
        as_short_dict=lambda: {"run_id": "r1"},
        as_extended_dict=lambda: {
            "run_id": "r1",
            "trigger": {},
            "actions": [{}],
            "config": {"alias": "big config"},
            "blueprint_inputs": {"some": "data"},
        },
    )
    hass.data[DATA_TRACE] = {"automation": {"abc": {"r1": fake_run}}}

    result = await tools.call(
        "get_automation_trace",
        {"automation_id": "abc", "run_id": "r1"},
        sid,
    )
    assert "trace" in result
    assert result["trace"]["run_id"] == "r1"
    assert "config" not in result["trace"], "config should be stripped to save tokens"
    assert "blueprint_inputs" not in result["trace"]


async def test_get_trace_missing(setup, hass):
    tools, _, sid = setup
    result = await tools.call(
        "get_automation_trace",
        {"automation_id": "nope", "run_id": "r99"},
        sid,
    )
    assert "error" in result
