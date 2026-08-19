"""Script create/update/delete flow tests.

Mirrors test_automations.py: drives the ToolRegistry directly with a
temporary scripts.yaml in the pytest hass config dir.
"""
from __future__ import annotations

import os

import pytest
import yaml

from custom_components.claude_chat.storage import SessionStore
from custom_components.claude_chat.tools import ToolRegistry


@pytest.fixture
async def setup(hass):
    path = hass.config.path("scripts.yaml")
    if os.path.exists(path):
        os.remove(path)
    store = SessionStore(hass)
    await store.async_load()
    session = await store.create()
    return ToolRegistry(hass, store), store, session.id


def _scripts_path(hass):
    return hass.config.path("scripts.yaml")


def _read(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _register_fake_reload(hass):
    """script.reload that mimics HA: re-seed entities from the file."""

    async def fake_reload(call):
        written = _read(_scripts_path(hass))
        for script_id in written:
            hass.states.async_set(f"script.{script_id}", "off")

    hass.services.async_register("script", "reload", fake_reload)


async def test_propose_create_stages_change(setup, hass):
    tools, store, sid = setup
    result = await tools.call(
        "propose_script_create",
        {
            "script_id": "good_morning",
            "config": {"alias": "Good Morning", "sequence": [{"service": "light.turn_on"}]},
            "summary": "morning script",
        },
        sid,
    )
    assert "pending_change_id" in result
    session = store.get_or_raise(sid)
    assert len(session.pending_changes) == 1
    change = session.pending_changes[0]
    assert change.kind == "script_create"
    assert "alias: Good Morning" in change.payload["yaml"]


async def test_propose_create_rejects_non_slug_id(setup, hass):
    tools, _, sid = setup
    result = await tools.call(
        "propose_script_create",
        {"script_id": "Good Morning!", "config": {"sequence": []}, "summary": "x"},
        sid,
    )
    assert "error" in result and "slug" in result["error"]


async def test_propose_create_rejects_existing_id(setup, hass):
    tools, _, sid = setup
    with open(_scripts_path(hass), "w") as f:
        yaml.safe_dump({"exists": {"sequence": []}}, f)
    result = await tools.call(
        "propose_script_create",
        {"script_id": "exists", "config": {"sequence": []}, "summary": "x"},
        sid,
    )
    assert "error" in result and "propose_script_update" in result["error"]


async def test_apply_create_writes_file_and_calls_reload(setup, hass):
    tools, store, sid = setup
    _register_fake_reload(hass)
    await tools.call(
        "propose_script_create",
        {
            "script_id": "sunset",
            "config": {"alias": "Sunset", "sequence": [{"service": "notify.notify"}]},
            "summary": "x",
        },
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[0]
    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result

    written = _read(_scripts_path(hass))
    assert written["sunset"]["alias"] == "Sunset"


async def test_apply_create_warns_when_include_missing(setup, hass):
    tools, store, sid = setup

    async def silent_reload(call):
        return None

    hass.services.async_register("script", "reload", silent_reload)

    await tools.call(
        "propose_script_create",
        {"script_id": "ghost", "config": {"sequence": []}, "summary": "x"},
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[0]
    apply_result = await tools.apply_pending_change(change)
    assert "error" in apply_result
    assert "!include scripts.yaml" in apply_result["error"]


async def test_update_requires_existing_id(setup, hass):
    tools, _, sid = setup
    result = await tools.call(
        "propose_script_update",
        {"script_id": "nope", "config": {"sequence": []}, "summary": "x"},
        sid,
    )
    assert "error" in result and "not found" in result["error"].lower()


async def test_update_replaces_and_keeps_others(setup, hass):
    tools, store, sid = setup
    _register_fake_reload(hass)
    with open(_scripts_path(hass), "w") as f:
        yaml.safe_dump(
            {
                "keep": {"alias": "Keep", "sequence": []},
                "target": {"alias": "Old", "sequence": []},
            },
            f,
        )
    result = await tools.call(
        "propose_script_update",
        {
            "script_id": "target",
            "config": {"alias": "New", "sequence": [{"delay": "00:00:05"}]},
            "summary": "rename",
        },
        sid,
    )
    assert "pending_change_id" in result
    change = store.get_or_raise(sid).pending_changes[0]
    assert change.diff and "New" in change.diff
    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result

    written = _read(_scripts_path(hass))
    assert written["target"]["alias"] == "New"
    assert written["keep"]["alias"] == "Keep"


async def test_delete_removes_entry(setup, hass):
    tools, store, sid = setup
    _register_fake_reload(hass)
    with open(_scripts_path(hass), "w") as f:
        yaml.safe_dump(
            {"keep": {"sequence": []}, "gone": {"sequence": []}}, f
        )
    await tools.call(
        "propose_script_delete", {"script_id": "gone", "summary": "x"}, sid
    )
    change = store.get_or_raise(sid).pending_changes[0]
    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result
    assert list(_read(_scripts_path(hass))) == ["keep"]


async def test_second_propose_supersedes_first_for_same_script(setup, hass):
    tools, store, sid = setup
    with open(_scripts_path(hass), "w") as f:
        yaml.safe_dump({"s1": {"sequence": []}}, f)
    await tools.call(
        "propose_script_update",
        {"script_id": "s1", "config": {"sequence": []}, "summary": "first"},
        sid,
    )
    await tools.call(
        "propose_script_update",
        {"script_id": "s1", "config": {"sequence": []}, "summary": "second"},
        sid,
    )
    session = store.get_or_raise(sid)
    assert len(session.pending_changes) == 1
    assert session.pending_changes[0].summary == "second"


async def test_get_script_finds_by_id(setup, hass):
    tools, _, sid = setup
    with open(_scripts_path(hass), "w") as f:
        yaml.safe_dump({"found": {"alias": "Found", "sequence": []}}, f)
    result = await tools.call("get_script", {"script_id": "found"}, sid)
    assert result["config"]["alias"] == "Found"

    missing = await tools.call("get_script", {"script_id": "nope"}, sid)
    assert "error" in missing
    assert missing["available_script_ids"] == ["found"]
