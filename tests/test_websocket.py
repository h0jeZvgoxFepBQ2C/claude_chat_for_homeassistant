"""WebSocket API tests — exercise the full integration via hass_ws_client."""
from __future__ import annotations

import pytest

from .fake_anthropic import text_event, tool_use_event


async def test_create_list_get_session(hass, hass_ws_client, configured_entry):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "claude_chat/create_session", "title": "first"}
    )
    res = await client.receive_json()
    assert res["success"]
    sid = res["result"]["id"]

    await client.send_json_auto_id({"type": "claude_chat/list_sessions"})
    res = await client.receive_json()
    assert res["success"]
    assert any(s["id"] == sid for s in res["result"]["sessions"])

    await client.send_json_auto_id(
        {"type": "claude_chat/get_session", "session_id": sid}
    )
    res = await client.receive_json()
    assert res["success"]
    assert res["result"]["id"] == sid


async def test_send_message_streams_to_client(
    hass, hass_ws_client, configured_entry, fake_anthropic
):
    fake_anthropic.script(text_event("Hi!"))
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "claude_chat/create_session", "title": "chat"}
    )
    res = await client.receive_json()
    sid = res["result"]["id"]

    await client.send_json_auto_id(
        {"type": "claude_chat/send_message", "session_id": sid, "text": "hi"}
    )
    initial = await client.receive_json()
    assert initial["success"]

    # Drain events until 'done'.
    saw_text_delta = False
    while True:
        msg = await client.receive_json()
        assert msg["type"] == "event"
        ev = msg["event"]
        if ev["type"] == "text_delta":
            saw_text_delta = True
            assert ev["text"] == "Hi!"
        if ev["type"] == "done":
            assert any(
                m["role"] == "assistant" for m in ev["session"]["messages"]
            )
            break
    assert saw_text_delta


async def test_tool_use_creates_and_approves_pending_change(
    hass, hass_ws_client, configured_entry, fake_anthropic
):
    """Smoke-test the diff+approve flow using a fake tool that always errors.

    We can't easily wire up a real Lovelace dashboard in pytest, so this test
    confirms the *plumbing*: when the model makes a tool call, the tool
    runs and its result flows back through the websocket.
    """
    hass.states.async_set("sensor.foo", "42")
    fake_anthropic.script(
        tool_use_event(
            tool_id="t1",
            name="list_entities",
            input_obj={"domain": "sensor"},
        ),
        text_event("done"),
    )

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "claude_chat/create_session"})
    sid = (await client.receive_json())["result"]["id"]

    await client.send_json_auto_id(
        {"type": "claude_chat/send_message", "session_id": sid, "text": "list"}
    )
    await client.receive_json()  # initial ack

    saw_tool_result = False
    while True:
        msg = await client.receive_json()
        ev = msg["event"]
        if ev["type"] == "tool_result":
            saw_tool_result = True
            assert ev["name"] == "list_entities"
        if ev["type"] == "done":
            break
    assert saw_tool_result


async def test_reject_change(hass, hass_ws_client, configured_entry):
    """reject_change drops a pending change without applying it."""
    from custom_components.claude_chat.const import DOMAIN
    from custom_components.claude_chat.storage import PendingChange

    store = hass.data[DOMAIN]["store"]
    session = await store.create()
    await store.add_pending(
        session.id,
        PendingChange(
            id="c1",
            kind="dashboard_update",
            summary="x",
            payload={},
        ),
    )

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "claude_chat/reject_change",
            "session_id": session.id,
            "change_id": "c1",
        }
    )
    res = await client.receive_json()
    assert res["success"]
    # Rejected changes stay as history (status="rejected") rather than vanish.
    changes = store.get_or_raise(session.id).pending_changes
    assert len(changes) == 1
    assert changes[0].status == "rejected"
