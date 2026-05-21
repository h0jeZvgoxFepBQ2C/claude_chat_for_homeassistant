"""End-to-end tool-use loop test against the FakeAsyncAnthropic."""
from __future__ import annotations

from custom_components.claude_chat.claude_client import ClaudeClient
from custom_components.claude_chat.storage import Message, SessionStore
from custom_components.claude_chat.tools import ToolRegistry

from .fake_anthropic import (
    FakeAsyncAnthropic,
    text_event,
    tool_use_event,
    truncated_text_event,
    truncated_mid_tool_use_event,
)


async def test_simple_text_response(hass):
    fake = FakeAsyncAnthropic().script(text_event("Hello back!"))
    store = SessionStore(hass)
    await store.async_load()
    tools = ToolRegistry(hass, store)
    session = await store.create()

    client = ClaudeClient.__new__(ClaudeClient)
    client._client = fake
    client._default_model = "test"
    client._tools = tools

    events: list[dict] = []

    async def emit(e):
        events.append(e)

    history = [Message(role="user", content=[{"type": "text", "text": "hi"}])]
    new_messages = await client.stream_chat(history, session.id, emit)

    assert any(e["type"] == "text_delta" and e["text"] == "Hello back!" for e in events)
    assert any(e["type"] == "turn_complete" for e in events)
    # One assistant message appended (no tool round-trip).
    assert len(new_messages) == 1
    assert new_messages[0].role == "assistant"
    assert new_messages[0].content[0]["type"] == "text"


async def test_tool_use_loop(hass):
    """Model calls list_entities, then replies with text."""
    hass.states.async_set("sensor.foo", "42")

    fake = FakeAsyncAnthropic().script(
        tool_use_event(
            tool_id="toolu_1",
            name="list_entities",
            input_obj={"domain": "sensor"},
            preceding_text="Let me check.",
        ),
        text_event("Found 1 sensor."),
    )

    store = SessionStore(hass)
    await store.async_load()
    tools = ToolRegistry(hass, store)
    session = await store.create()

    client = ClaudeClient.__new__(ClaudeClient)
    client._client = fake
    client._default_model = "test"
    client._tools = tools

    events: list[dict] = []

    async def emit(e):
        events.append(e)

    history = [Message(role="user", content=[{"type": "text", "text": "list sensors"}])]
    new_messages = await client.stream_chat(history, session.id, emit)

    # Verify the loop went: assistant(tool_use) → user(tool_result) → assistant(text)
    assert len(new_messages) == 3
    assert new_messages[0].role == "assistant"
    assert any(b["type"] == "tool_use" for b in new_messages[0].content)
    assert new_messages[1].role == "user"
    assert new_messages[1].content[0]["type"] == "tool_result"
    assert new_messages[2].role == "assistant"

    # Tool result event was emitted with our actual entity in the payload.
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["name"] == "list_entities"
    assert any(
        ent["entity_id"] == "sensor.foo" for ent in tool_result["result"]["entities"]
    )


async def test_max_tokens_truncated_before_tool_use(hass):
    """max_tokens hit while streaming text, before a tool_use block starts.

    Regression for the 'Claude says it'll add the card but never proposes'
    bug: the response ends with stop_reason=max_tokens (no content_block_stop),
    leaving the partial text in current_block. The client must:
      - flush the partial text to the saved message
      - emit an error event (not turn_complete)
      - NOT emit a tool result or pending change
    """
    fake = FakeAsyncAnthropic().script(
        truncated_text_event("Ich füge die auto-entities Karte ganz oben ein:"),
    )
    store = SessionStore(hass)
    await store.async_load()
    tools = ToolRegistry(hass, store)
    session = await store.create()

    client = ClaudeClient.__new__(ClaudeClient)
    client._client = fake
    client._default_model = "test"
    client._tools = tools

    events: list[dict] = []

    async def emit(e):
        events.append(e)

    history = [Message(role="user", content=[{"type": "text", "text": "add a card"}])]
    new_messages = await client.stream_chat(history, session.id, emit)

    # Must surface an error — not silently complete.
    error_events = [e for e in events if e["type"] == "error"]
    assert error_events, "Expected an error event when max_tokens is hit"
    assert "max_tokens" in error_events[0]["error"].lower() or "truncated" in error_events[0]["error"].lower()

    # No turn_complete (that would signal a normal end).
    assert not any(e["type"] == "turn_complete" for e in events)

    # The partial text is saved so the conversation history is not empty.
    assert len(new_messages) == 1
    assert new_messages[0].role == "assistant"
    saved_text = next(
        (b["text"] for b in new_messages[0].content if b.get("type") == "text"), None
    )
    assert saved_text and "auto-entities" in saved_text


async def test_max_tokens_truncated_mid_tool_use(hass):
    """max_tokens hit while streaming a tool_use input JSON.

    The tool_use_start event fired (chip appeared in UI) but the JSON was
    never completed, so the block must be discarded — the tool must NOT be
    called and no pending change must be created.
    """
    fake = FakeAsyncAnthropic().script(
        truncated_mid_tool_use_event(
            preceding_text="Hier kommt der Vorschlag:",
            tool_id="toolu_trunc",
            name="propose_dashboard_update",
        ),
    )
    store = SessionStore(hass)
    await store.async_load()
    tools = ToolRegistry(hass, store)
    session = await store.create()

    client = ClaudeClient.__new__(ClaudeClient)
    client._client = fake
    client._default_model = "test"
    client._tools = tools

    events: list[dict] = []

    async def emit(e):
        events.append(e)

    history = [Message(role="user", content=[{"type": "text", "text": "add a card"}])]
    new_messages = await client.stream_chat(history, session.id, emit)

    # Error emitted, no turn_complete.
    assert any(e["type"] == "error" for e in events)
    assert not any(e["type"] == "turn_complete" for e in events)

    # Partial tool_use was discarded — no tool_result event, no pending change.
    assert not any(e["type"] == "tool_result" for e in events)
    refreshed = store.get_or_raise(session.id)
    assert not refreshed.pending_changes

    # Preceding text was saved, tool_use was NOT saved.
    assert len(new_messages) == 1
    content_types = [b["type"] for b in new_messages[0].content]
    assert "text" in content_types
    assert "tool_use" not in content_types
