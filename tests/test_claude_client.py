"""End-to-end tool-use loop test against the FakeAsyncAnthropic."""
from __future__ import annotations

from custom_components.claude_chat.claude_client import ClaudeClient
from custom_components.claude_chat.storage import Message, SessionStore
from custom_components.claude_chat.tools import ToolRegistry

from .fake_anthropic import FakeAsyncAnthropic, text_event, tool_use_event


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
