"""www file-write and Lovelace resource-add flow tests."""
from __future__ import annotations

import os
import shutil

import pytest
from homeassistant.setup import async_setup_component

from custom_components.claude_chat.storage import SessionStore
from custom_components.claude_chat.tools import ToolRegistry


@pytest.fixture
async def setup(hass):
    www = hass.config.path("www")
    if os.path.isdir(www):
        shutil.rmtree(www)
    store = SessionStore(hass)
    await store.async_load()
    session = await store.create()
    return ToolRegistry(hass, store), store, session.id


# --- file_write ------------------------------------------------------------


async def test_propose_and_apply_file_write_creates_file(setup, hass):
    tools, store, sid = setup
    result = await tools.call(
        "propose_file_write",
        {"path": "cards/my-card.js", "content": "console.log('hi')", "summary": "card"},
        sid,
    )
    assert result["status"] == "awaiting_user_approval"
    assert result["local_url"] == "/local/cards/my-card.js"

    change = store.get_or_raise(sid).pending_changes[0]
    assert change.kind == "file_write"
    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result

    with open(hass.config.path("www/cards/my-card.js")) as f:
        assert f.read() == "console.log('hi')"


async def test_file_write_rejects_path_escape(setup, hass):
    tools, _, sid = setup
    for bad in ("../secrets.yaml", "/etc/passwd", "..", "a/../../x.js", ""):
        result = await tools.call(
            "propose_file_write", {"path": bad, "content": "x", "summary": "s"}, sid
        )
        assert "error" in result, f"path {bad!r} should be rejected"


async def test_file_write_accepts_local_url_prefix(setup, hass):
    tools, store, sid = setup
    await tools.call(
        "propose_file_write",
        {"path": "/local/foo.js", "content": "x", "summary": "s"},
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[0]
    assert change.payload["path"] == "foo.js"


async def test_file_write_diff_for_existing_file(setup, hass):
    tools, store, sid = setup
    os.makedirs(hass.config.path("www"), exist_ok=True)
    with open(hass.config.path("www/x.js"), "w") as f:
        f.write("old\n")
    await tools.call(
        "propose_file_write", {"path": "x.js", "content": "new\n", "summary": "s"}, sid
    )
    change = store.get_or_raise(sid).pending_changes[0]
    assert change.diff and "-old" in change.diff and "+new" in change.diff


async def test_get_file_and_list_files(setup, hass):
    tools, _, sid = setup
    os.makedirs(hass.config.path("www/sub"), exist_ok=True)
    with open(hass.config.path("www/sub/a.js"), "w") as f:
        f.write("abc")

    listed = await tools.call("list_files", {}, sid)
    assert [f["path"] for f in listed["files"]] == ["sub/a.js"]

    read = await tools.call("get_file", {"path": "sub/a.js"}, sid)
    assert read["content"] == "abc"

    missing = await tools.call("get_file", {"path": "nope.js"}, sid)
    assert "error" in missing

    escape = await tools.call("get_file", {"path": "../configuration.yaml"}, sid)
    assert "error" in escape


# --- resource_add ----------------------------------------------------------


@pytest.fixture
async def lovelace_setup(setup, hass):
    assert await async_setup_component(hass, "lovelace", {})
    return setup


async def test_propose_and_apply_resource_add(lovelace_setup, hass):
    tools, store, sid = lovelace_setup
    result = await tools.call(
        "propose_resource_add",
        {"url": "/local/my-card.js", "res_type": "module", "summary": "register card"},
        sid,
    )
    assert result["status"] == "awaiting_user_approval"

    change = store.get_or_raise(sid).pending_changes[0]
    assert change.kind == "resource_add"
    apply_result = await tools.apply_pending_change(change)
    assert apply_result.get("ok"), apply_result

    listed = await tools.call("list_lovelace_resources", {}, sid)
    assert any(r["url"] == "/local/my-card.js" for r in listed["resources"])


async def test_resource_add_rejects_duplicate(lovelace_setup, hass):
    tools, store, sid = lovelace_setup
    await tools.call(
        "propose_resource_add",
        {"url": "/local/dup.js", "res_type": "module", "summary": "x"},
        sid,
    )
    change = store.get_or_raise(sid).pending_changes[0]
    assert (await tools.apply_pending_change(change)).get("ok")

    result = await tools.call(
        "propose_resource_add",
        {"url": "/local/dup.js", "res_type": "module", "summary": "again"},
        sid,
    )
    assert "error" in result and "already" in result["error"]


async def test_resource_add_rejects_bad_type(lovelace_setup, hass):
    tools, _, sid = lovelace_setup
    result = await tools.call(
        "propose_resource_add",
        {"url": "/local/x.js", "res_type": "wasm", "summary": "x"},
        sid,
    )
    assert "error" in result
