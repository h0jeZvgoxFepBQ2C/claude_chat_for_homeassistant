"""Pending-change history preservation tests.

After v0.3.0, approve / reject no longer remove a PendingChange — they
flip its `status` to "accepted" / "rejected" so the conversation
history stays complete.
"""
from __future__ import annotations

import pytest

from custom_components.claude_chat.storage import PendingChange, SessionStore
from custom_components.claude_chat.tools import ToolRegistry


@pytest.fixture
async def setup(hass):
    store = SessionStore(hass)
    await store.async_load()
    session = await store.create()
    return ToolRegistry(hass, store), store, session.id


async def test_reject_marks_status_keeps_entry(setup):
    tools, store, sid = setup
    await store.add_pending(
        sid,
        PendingChange(id="c1", kind="dashboard_update", summary="x", payload={}),
    )
    await store.set_change_status(sid, "c1", "rejected")
    session = store.get_or_raise(sid)
    assert len(session.pending_changes) == 1
    assert session.pending_changes[0].status == "rejected"


async def test_accept_status_keeps_entry(setup):
    _, store, sid = setup
    await store.add_pending(
        sid,
        PendingChange(id="c1", kind="dashboard_update", summary="x", payload={}),
    )
    await store.set_change_status(sid, "c1", "accepted")
    session = store.get_or_raise(sid)
    assert len(session.pending_changes) == 1
    assert session.pending_changes[0].status == "accepted"


async def test_supersede_skips_non_pending_history(setup):
    """When a new pending change is added for the same target, an *active
    pending* sibling gets replaced — but accepted/rejected history stays."""
    tools, store, sid = setup
    # Seed an already-accepted history entry + a pending sibling.
    await store.add_pending(
        sid,
        PendingChange(
            id="hist",
            kind="dashboard_update",
            summary="first",
            payload={"url_path": "lovelace", "new_config": {}},
        ),
    )
    await store.set_change_status(sid, "hist", "accepted")
    await store.add_pending(
        sid,
        PendingChange(
            id="active",
            kind="dashboard_update",
            summary="second (pending)",
            payload={"url_path": "lovelace", "new_config": {}},
        ),
    )

    # Add a third proposal for the same dashboard via the supersede helper.
    new = PendingChange(
        id="newer",
        kind="dashboard_update",
        summary="third (latest)",
        payload={"url_path": "lovelace", "new_config": {"views": []}},
    )
    await tools._add_pending_supersede(sid, new)

    session = store.get_or_raise(sid)
    by_id = {c.id: c for c in session.pending_changes}
    # The pending sibling is gone (superseded), but accepted history stays.
    assert "active" not in by_id, "pending sibling should be superseded"
    assert by_id["hist"].status == "accepted"
    assert by_id["newer"].status == "pending"
    assert len(session.pending_changes) == 2


async def test_list_sessions_has_pending_only_counts_pending(setup, hass):
    _, store, sid = setup
    await store.add_pending(
        sid,
        PendingChange(id="c1", kind="x", summary="x", payload={}),
    )
    await store.set_change_status(sid, "c1", "accepted")
    sessions = store.list_sessions()
    s = next(x for x in sessions if x["id"] == sid)
    assert s["has_pending"] is False
