"""Storage layer is pure-python, no HA bootstrap needed."""
from __future__ import annotations

import pytest

from custom_components.claude_chat.storage import Message, PendingChange, SessionStore


@pytest.fixture
def store(hass):
    return SessionStore(hass)


async def test_create_and_list(store: SessionStore):
    await store.async_load()
    s = await store.create("first")
    assert s.title == "first"
    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["id"] == s.id
    assert sessions[0]["message_count"] == 0


async def test_append_message_and_persist(hass, store: SessionStore):
    await store.async_load()
    s = await store.create()
    await store.append_message(
        s.id,
        Message(role="user", content=[{"type": "text", "text": "hi"}]),
    )
    # Re-instantiate to verify round-trip via the Store helper.
    fresh = SessionStore(hass)
    await fresh.async_load()
    sessions = fresh.list_sessions()
    assert sessions[0]["message_count"] == 1
    loaded = fresh.get(s.id)
    assert loaded is not None
    assert loaded.messages[0].content[0]["text"] == "hi"


async def test_pending_changes(store: SessionStore):
    await store.async_load()
    s = await store.create()
    change = PendingChange(
        id="c1",
        kind="dashboard_update",
        summary="add living room card",
        payload={"url_path": "lovelace", "new_config": {}},
        diff="--- a\n+++ b\n",
    )
    await store.add_pending(s.id, change)
    refreshed = store.get_or_raise(s.id)
    assert refreshed.pending_changes[0].id == "c1"

    removed = await store.remove_pending(s.id, "c1")
    assert removed is not None
    assert store.get_or_raise(s.id).pending_changes == []


async def test_delete(store: SessionStore):
    await store.async_load()
    s = await store.create()
    await store.delete(s.id)
    assert store.get(s.id) is None
