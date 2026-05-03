"""Unit tests for the ToolRegistry — drives tools directly, no Anthropic involved."""
from __future__ import annotations

import pytest

from custom_components.claude_chat.storage import SessionStore
from custom_components.claude_chat.tools import ToolRegistry


@pytest.fixture
async def setup(hass):
    # Seed a couple of states so list_entities has something to return.
    hass.states.async_set("sensor.living_room_temperature", "21.4", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.bedroom_temperature", "19.2", {"unit_of_measurement": "°C"})
    hass.states.async_set("light.kitchen", "on")
    store = SessionStore(hass)
    await store.async_load()
    session = await store.create()
    return ToolRegistry(hass, store), store, session.id


async def test_list_entities_no_filter(setup):
    tools, _, sid = setup
    result = await tools.call("list_entities", {}, sid)
    ids = {e["entity_id"] for e in result["entities"]}
    assert "sensor.living_room_temperature" in ids
    assert "light.kitchen" in ids


async def test_list_entities_domain_filter(setup):
    tools, _, sid = setup
    result = await tools.call("list_entities", {"domain": "sensor"}, sid)
    domains = {e["entity_id"].split(".")[0] for e in result["entities"]}
    assert domains == {"sensor"}


async def test_get_entity(setup):
    tools, _, sid = setup
    result = await tools.call(
        "get_entity", {"entity_id": "sensor.living_room_temperature"}, sid
    )
    assert result["state"] == "21.4"
    assert result["attributes"]["unit_of_measurement"] == "°C"


async def test_get_entity_not_found(setup):
    tools, _, sid = setup
    result = await tools.call("get_entity", {"entity_id": "sensor.nope"}, sid)
    assert "error" in result


async def test_unknown_tool(setup):
    tools, _, sid = setup
    result = await tools.call("does_not_exist", {}, sid)
    assert "Unknown tool" in result["error"]


async def test_propose_dashboard_update_no_dashboard(setup):
    """When there's no Lovelace data, proposing should return an error rather than crash."""
    tools, _, sid = setup
    result = await tools.call(
        "propose_dashboard_update",
        {"new_config": {"views": []}, "summary": "test"},
        sid,
    )
    assert "error" in result
