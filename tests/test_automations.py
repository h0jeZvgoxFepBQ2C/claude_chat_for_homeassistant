"""Automation create/update/delete flow tests.

Drives the ToolRegistry directly, with a temporary automations.yaml living
in the hass config dir provided by pytest-homeassistant-custom-component.
"""
from __future__ import annotations

import os

import pytest
import yaml

from custom_components.claude_chat.storage import SessionStore
from custom_components.claude_chat.tools import ToolRegistry


@pytest.fixture
async def setup(hass):
    # Make sure each test starts with a clean automations.yaml — pytest's
    # hass fixture can reuse a config dir within a session.
    path = hass.config.path("automations.yaml")
    if os.path.exists(path):
        os.remove(path)
    store = SessionStore(hass)
    await store.async_load()
    session = await store.create()
    return ToolRegistry(hass, store), store, session.id


def _automations_path(hass):
    return hass.config.path("automations.yaml")


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return yaml.safe_load(f) or []


async def test_propose_create_stages_change(setup, hass):
    tools, store, sid = setup
    config = {
        "alias": "Test At Sunset",
        "trigger": [{"platform": "state", "entity_id": "sun.sun", "to": "below_horizon"}],
        "action": [{"service": "notify.notify", "data": {"message": "Sunset"}}],
    }
    result = await tools.call(
        "propose_automation_create",
        {"config": config, "summary": "notify on sunset"},
        sid,
    )
    assert "pending_change_id" in result
    session = store.get_or_raise(sid)
    assert len(session.pending_changes) == 1
    assert session.pending_changes[0].kind == "automation_create"
    # YAML preview was generated for the UI.
    assert "alias: Test At Sunset" in session.pending_changes[0].payload["yaml"]


async def test_apply_create_writes_file_and_calls_reload(setup, hass):
    tools, store, sid = setup
    reload_called = []

    async def fake_reload(call):
        # Mimic HA: after reload, the new automation appears as an entity.
        reload_called.append(call)
        written = _read(_automations_path(hass))
        for a in written:
            entity_id = f"automation.{a['alias'].lower().replace(' ', '_')}"
            hass.states.async_set(entity_id, "on", {"id": str(a["id"])})

    hass.services.async_register("automation", "reload", fake_reload)

    config = {
        "alias": "Sunset",
        "trigger": [{"platform": "state", "entity_id": "sun.sun"}],
        "action": [{"service": "notify.notify"}],
    }
    result = await tools.call(
        "propose_automation_create",
        {"config": config, "summary": "x"},
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[0]

    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result

    written = _read(_automations_path(hass))
    assert len(written) == 1
    assert written[0]["alias"] == "Sunset"
    assert written[0].get("id"), "id should be auto-assigned"
    assert reload_called, "automation.reload should be called"


async def test_apply_create_warns_when_include_missing(setup, hass):
    """If automation.reload doesn't load the new entity, surface a clear error."""
    tools, store, sid = setup

    # Reload is a no-op — simulating a configuration.yaml without the
    # `automation: !include automations.yaml` line.
    async def silent_reload(call):
        return None

    hass.services.async_register("automation", "reload", silent_reload)

    result = await tools.call(
        "propose_automation_create",
        {"config": {"alias": "X", "trigger": [], "action": []}, "summary": "x"},
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[0]
    apply_result = await tools.apply_pending_change(change)
    assert "error" in apply_result
    assert "!include automations.yaml" in apply_result["error"]


async def test_update_requires_existing_id(setup, hass):
    tools, _, sid = setup
    result = await tools.call(
        "propose_automation_update",
        {
            "automation_id": "nope",
            "config": {"alias": "x", "trigger": [], "action": []},
            "summary": "x",
        },
        sid,
    )
    assert "error" in result and "not found" in result["error"].lower()


async def test_update_replaces_in_place(setup, hass):
    tools, store, sid = setup

    async def fake_reload(call):
        # Re-seed entity states from the file post-reload.
        written = _read(_automations_path(hass))
        for a in written:
            entity_id = f"automation.{a['alias'].lower().replace(' ', '_')}"
            hass.states.async_set(entity_id, "on", {"id": str(a["id"])})

    hass.services.async_register("automation", "reload", fake_reload)

    # Seed an existing automation in the file AND in hass.states.
    path = _automations_path(hass)
    with open(path, "w") as f:
        yaml.safe_dump(
            [{"id": "abc", "alias": "Old", "trigger": [], "action": []}], f
        )
    hass.states.async_set("automation.old", "on", {"id": "abc"})

    new = {"alias": "New", "trigger": [{"platform": "time", "at": "08:00"}], "action": []}
    result = await tools.call(
        "propose_automation_update",
        {"automation_id": "abc", "config": new, "summary": "rename"},
        sid,
    )
    assert "pending_change_id" in result
    change = store.get_or_raise(sid).pending_changes[0]
    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result

    written = _read(path)
    assert written[0]["alias"] == "New"
    assert written[0]["id"] == "abc"
    assert written[0]["trigger"][0]["platform"] == "time"


async def test_delete_removes_entry(setup, hass):
    tools, store, sid = setup

    async def fake_reload(call):
        return None

    hass.services.async_register("automation", "reload", fake_reload)

    path = _automations_path(hass)
    with open(path, "w") as f:
        yaml.safe_dump(
            [
                {"id": "a", "alias": "Keep", "trigger": [], "action": []},
                {"id": "b", "alias": "Delete me", "trigger": [], "action": []},
            ],
            f,
        )

    result = await tools.call(
        "propose_automation_delete",
        {"automation_id": "b", "summary": "remove unused"},
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[0]
    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result

    written = _read(path)
    assert len(written) == 1
    assert written[0]["id"] == "a"


async def test_get_automation_finds_by_id(setup, hass):
    tools, _, sid = setup
    path = _automations_path(hass)
    with open(path, "w") as f:
        yaml.safe_dump(
            [{"id": "xyz", "alias": "Found", "trigger": [], "action": []}], f
        )

    result = await tools.call(
        "get_automation", {"automation_id": "xyz"}, sid
    )
    assert result["config"]["alias"] == "Found"
