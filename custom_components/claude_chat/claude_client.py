"""Anthropic API client wrapper with streaming + tool-use loop."""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from anthropic import AsyncAnthropic

from .const import DEFAULT_MAX_TOKENS, MAX_TURNS_PER_REQUEST
from .storage import Message
from .tools import TOOL_DEFINITIONS, ToolRegistry

_LOGGER = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an in-house assistant inside a Home Assistant installation.

You help the user inspect their smart home, modify their Lovelace dashboards,
and trigger services.

Rules:
- Inspect before you edit. Use list_dashboards / get_dashboard before \
  proposing an update. Use list_entities / get_entity to discover the right \
  entity_ids. Use list_lovelace_resources to see if custom cards (mushroom, \
  mini-graph-card, etc.) are available — only use them if they're listed.
- propose_dashboard_update, propose_service_call, propose_automation_create, \
  propose_automation_update, and propose_automation_delete all STAGE changes \
  for the user to review. They do NOT apply immediately. Do not say "done" \
  until the user has approved.
- For automations: use list_automations to find an automation_id, then \
  get_automation to read the full config before propose_automation_update. \
  Build configs as dicts (e.g. {"alias": "...", "trigger": [{"platform": \
  "state", ...}], "action": [{"service": "..."}]}).
- When proposing a dashboard update, ALWAYS send the complete dashboard config, \
  not a partial patch. Fetch the current config, modify it, send the whole \
  thing back. If get_dashboard returns an empty skeleton, the dashboard is \
  auto-generated and your save will create it.
- Prefer minimal changes. If the user asks for "a widget for sensor X", add \
  one card to the appropriate view, don't restructure the dashboard.
- Be concise. Don't narrate every tool call — just do them and report the \
  result. Use markdown for tables and code blocks when it aids clarity.
"""

TITLE_PROMPT = (
    "Summarize this user request as a 4-6 word chat title. No quotes, no "
    "punctuation at the end, sentence case. Just the title."
)


# Event types yielded by stream_chat
EventType = dict[str, Any]
EventEmitter = Callable[[EventType], Awaitable[None]]


AVAILABLE_MODELS = [
    {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5"},
    {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6"},
    {"id": "claude-opus-4-7", "name": "Opus 4.7"},
]


class ClaudeClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        tools: ToolRegistry,
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._default_model = model
        self._tools = tools

    @property
    def default_model(self) -> str:
        return self._default_model

    async def summarize_title(self, user_text: str) -> str:
        """Quick non-streaming call to title a chat session."""
        try:
            resp = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=30,
                system=TITLE_PROMPT,
                messages=[{"role": "user", "content": user_text[:500]}],
            )
            blocks = resp.content
            for b in blocks:
                if getattr(b, "type", None) == "text":
                    return b.text.strip().strip("\"'.,!?")[:60]
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Title summarization failed")
        return ""

    async def stream_chat(
        self,
        history: list[Message],
        session_id: str,
        emit: EventEmitter,
        model: str | None = None,
    ) -> list[Message]:
        """Run a tool-use loop, streaming events to `emit`.

        Returns the new messages appended (assistant turns + tool_result turns).
        """
        active_model = model or self._default_model
        api_messages = _to_api_messages(history)
        new_messages: list[Message] = []

        for turn in range(MAX_TURNS_PER_REQUEST):
            assistant_blocks: list[dict[str, Any]] = []
            stop_reason: str | None = None

            async with self._client.messages.stream(
                model=active_model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=api_messages,
            ) as stream:
                current_block_index: int | None = None
                current_block: dict[str, Any] | None = None
                tool_input_buffer: str = ""

                async for event in stream:
                    etype = event.type

                    if etype == "content_block_start":
                        current_block_index = event.index
                        block = event.content_block
                        if block.type == "text":
                            current_block = {"type": "text", "text": ""}
                        elif block.type == "tool_use":
                            current_block = {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": {},
                            }
                            tool_input_buffer = ""
                            await emit(
                                {
                                    "type": "tool_use_start",
                                    "id": block.id,
                                    "name": block.name,
                                }
                            )
                        else:
                            current_block = {"type": block.type}

                    elif etype == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta" and current_block:
                            current_block["text"] += delta.text
                            await emit({"type": "text_delta", "text": delta.text})
                        elif delta.type == "input_json_delta" and current_block:
                            tool_input_buffer += delta.partial_json

                    elif etype == "content_block_stop":
                        if current_block is not None:
                            if current_block["type"] == "tool_use":
                                import json

                                try:
                                    current_block["input"] = (
                                        json.loads(tool_input_buffer)
                                        if tool_input_buffer
                                        else {}
                                    )
                                except json.JSONDecodeError:
                                    current_block["input"] = {}
                            assistant_blocks.append(current_block)
                        current_block = None
                        current_block_index = None

                    elif etype == "message_delta":
                        if event.delta.stop_reason:
                            stop_reason = event.delta.stop_reason

                    elif etype == "message_stop":
                        pass

            assistant_msg = Message(role="assistant", content=assistant_blocks)
            new_messages.append(assistant_msg)
            api_messages.append({"role": "assistant", "content": assistant_blocks})

            if stop_reason != "tool_use":
                await emit({"type": "turn_complete"})
                break

            tool_uses = [b for b in assistant_blocks if b["type"] == "tool_use"]
            tool_result_blocks: list[dict[str, Any]] = []
            for tool_use in tool_uses:
                result = await self._tools.call(
                    tool_use["name"],
                    tool_use["input"],
                    session_id,
                    tool_use["id"],
                )
                await emit(
                    {
                        "type": "tool_result",
                        "id": tool_use["id"],
                        "name": tool_use["name"],
                        "result": result,
                    }
                )
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use["id"],
                        "content": _stringify_tool_result(result),
                    }
                )
            user_msg = Message(role="user", content=tool_result_blocks)
            new_messages.append(user_msg)
            api_messages.append({"role": "user", "content": tool_result_blocks})
        else:
            await emit(
                {
                    "type": "error",
                    "error": f"Hit max turn limit ({MAX_TURNS_PER_REQUEST})",
                }
            )

        return new_messages


def _to_api_messages(history: list[Message]) -> list[dict[str, Any]]:
    return [{"role": m.role, "content": m.content} for m in history]


def _stringify_tool_result(result: Any) -> str:
    import json

    return json.dumps(result, default=str)
