"""Input-helper create/update/delete flow tests.

Sets up the real input_boolean / input_number components so the ToolRegistry
can reach their storage collections the same way it does in production
(via the registered `<domain>/create` websocket handler).
"""
from __future__ import annotations

import pytest
from homeassistant.setup import async_setup_component

from custom_components.claude_chat.storage import SessionStore
from custom_components.claude_chat.tools import ToolRegistry


@pytest.fixture
async def setup(hass):
    assert await async_setup_component(hass, "input_boolean", {})
    assert await async_setup_component(hass, "input_number", {})
    store = SessionStore(hass)
    await store.async_load()
    session = await store.create()
    return ToolRegistry(hass, store), store, session.id


async def _create_helper(tools, store, sid, domain, config):
    """Stage + apply a helper_create; returns the new entity_id."""
    await tools.call(
        "propose_helper_create",
        {"domain": domain, "config": config, "summary": "x"},
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[-1]
    result = await tools.apply_pending_change(change)
    assert result.get("ok"), result
    await store.set_change_status(sid, change.id, "accepted")
    return result["entity_id"]


async def test_propose_create_stages_change(setup, hass):
    tools, store, sid = setup
    result = await tools.call(
        "propose_helper_create",
        {
            "domain": "input_boolean",
            "config": {"name": "Vacation Mode", "icon": "mdi:beach"},
            "summary": "vacation toggle",
        },
        sid,
    )
    assert "pending_change_id" in result
    change = store.get_or_raise(sid).pending_changes[0]
    assert change.kind == "helper_create"
    assert "Vacation Mode" in change.payload["yaml"]


async def test_propose_create_requires_name(setup, hass):
    tools, _, sid = setup
    result = await tools.call(
        "propose_helper_create",
        {"domain": "input_boolean", "config": {"icon": "mdi:x"}, "summary": "x"},
        sid,
    )
    assert "error" in result and "name" in result["error"]


async def test_propose_create_unknown_domain(setup, hass):
    tools, _, sid = setup
    result = await tools.call(
        "propose_helper_create",
        {"domain": "input_llama", "config": {"name": "x"}, "summary": "x"},
        sid,
    )
    assert "error" in result


async def test_apply_create_creates_live_entity(setup, hass):
    tools, store, sid = setup
    entity_id = await _create_helper(
        tools, store, sid, "input_boolean", {"name": "Party Mode"}
    )
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.name == "Party Mode"


async def test_apply_create_surfaces_validation_error(setup, hass):
    tools, store, sid = setup
    # input_number requires min and max — the collection schema rejects this.
    await tools.call(
        "propose_helper_create",
        {"domain": "input_number", "config": {"name": "Broken"}, "summary": "x"},
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[0]
    result = await tools.apply_pending_change(change)
    assert "error" in result


async def test_get_helper_returns_stored_config(setup, hass):
    tools, store, sid = setup
    entity_id = await _create_helper(
        tools, store, sid, "input_number",
        {"name": "Threshold", "min": 0, "max": 100, "step": 5},
    )
    result = await tools.call("get_helper", {"entity_id": entity_id}, sid)
    assert result["config"]["name"] == "Threshold"
    assert result["config"]["max"] == 100


async def test_get_helper_rejects_non_storage_entity(setup, hass):
    tools, _, sid = setup
    result = await tools.call(
        "get_helper", {"entity_id": "input_boolean.yaml_defined"}, sid
    )
    assert "error" in result

    result = await tools.call("get_helper", {"entity_id": "light.kitchen"}, sid)
    assert "error" in result


async def test_update_flow(setup, hass):
    tools, store, sid = setup
    entity_id = await _create_helper(
        tools, store, sid, "input_number",
        {"name": "Volume", "min": 0, "max": 10},
    )
    result = await tools.call(
        "propose_helper_update",
        {
            "entity_id": entity_id,
            "config": {"name": "Volume", "min": 0, "max": 20},
            "summary": "raise max",
        },
        sid,
    )
    assert "pending_change_id" in result
    change = store.get_or_raise(sid).pending_changes[-1]
    assert change.kind == "helper_update"
    assert change.diff and "20" in change.diff

    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["max"] == 20


async def test_delete_flow(setup, hass):
    tools, store, sid = setup
    entity_id = await _create_helper(
        tools, store, sid, "input_boolean", {"name": "Temp Flag"}
    )
    await tools.call(
        "propose_helper_delete", {"entity_id": entity_id, "summary": "x"}, sid
    )
    change = store.get_or_raise(sid).pending_changes[-1]
    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None


async def test_second_propose_supersedes_first_for_same_helper(setup, hass):
    tools, store, sid = setup
    entity_id = await _create_helper(
        tools, store, sid, "input_boolean", {"name": "Flag"}
    )
    for summary in ("first", "second"):
        await tools.call(
            "propose_helper_update",
            {"entity_id": entity_id, "config": {"name": "Flag"}, "summary": summary},
            sid,
        )
    pending = [
        c for c in store.get_or_raise(sid).pending_changes if c.status == "pending"
    ]
    assert len(pending) == 1
    assert pending[0].summary == "second"
