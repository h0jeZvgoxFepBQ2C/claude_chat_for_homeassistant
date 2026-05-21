"""Anthropic API client wrapper with streaming + tool-use loop."""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from anthropic import AsyncAnthropic

from .const import DEFAULT_MAX_TOKENS, MAX_TURNS_PER_REQUEST
from .media import to_api_content
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
- If you propose a revised change for the SAME target (same dashboard \
  url_path, same automation_id, etc.), the older pending change is \
  AUTOMATICALLY replaced — there is only ever one open proposal per target. \
  Never tell the user to "reject the old one first" or "ablehnen Sie die \
  alte Version" — that just confuses them. Just say what changed and ask \
  them to review.
- Each request includes a [Session state] block at the end of this system \
  prompt listing the status of past proposals (✓ applied, ✗ rejected, \
  ⏳ pending). This is INFORMATIONAL — it tells you what the user has \
  decided. It does NOT mean you should skip proposing new changes. \
  Use it only to (a) avoid saying "please confirm everything" when items \
  are already ✓ Applied, (b) reference past decisions if the user asks, \
  (c) avoid blindly re-proposing something identical that was just ✗ \
  Rejected. Never quote the block back at the user verbatim. If the user \
  asks for a new change, you MUST still call the corresponding propose_* \
  tool — describing the change in text without staging it is a bug.
- For automations: use list_automations to find an automation_id, then \
  get_automation to read the full config before propose_automation_update. \
  Build configs as dicts (e.g. {"alias": "...", "trigger": [{"platform": \
  "state", ...}], "action": [{"service": "..."}]}).
- For debugging: list_automation_traces shows recent runs of an automation \
  (success/failure, trigger, condition results). get_automation_trace shows \
  the full step-by-step for one run. get_state_history shows when an entity \
  changed state in the last N hours. When the user asks "why didn't X fire" \
  or "what happened when Y", start with these.
- When proposing a dashboard change, ALWAYS use propose_dashboard_view_update \
  (not propose_dashboard_update). Call get_dashboard(summary=true) to see \
  view paths, then get_dashboard_view to fetch the single view to modify, \
  then propose_dashboard_view_update with only that view. The backend merges \
  it into the full config at apply time — you never need to output the full \
  dashboard. Only use propose_dashboard_update for a brand-new \
  auto-generated dashboard that has no stored config yet (get_dashboard \
  returns an empty skeleton).
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
        api_messages = _to_api_messages(history, self._tools.hass, session_id)
        new_messages: list[Message] = []

        # Build system prompt — SYSTEM_PROMPT is static so it gets a cache
        # breakpoint; the session-state block (dynamic) goes in a second block
        # after the cache marker so the cache hit still applies to the static part.
        system_blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        state_block = _session_state_block(self._tools.store, session_id)
        if state_block:
            system_blocks.append({"type": "text", "text": state_block})

        # Mark the last tool definition so the entire tool list is cached.
        tools_with_cache = [*TOOL_DEFINITIONS]
        tools_with_cache[-1] = {**tools_with_cache[-1], "cache_control": {"type": "ephemeral"}}

        for turn in range(MAX_TURNS_PER_REQUEST):
            assistant_blocks: list[dict[str, Any]] = []
            stop_reason: str | None = None

            async with self._client.messages.stream(
                model=active_model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=system_blocks,
                tools=tools_with_cache,
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

            # If truncated mid-block, flush any completed text so it's saved,
            # but drop partial tool_use blocks (the tool was never called).
            if current_block is not None:
                if current_block["type"] == "text" and current_block.get("text"):
                    assistant_blocks.append(current_block)
                # Partial tool_use: discard — tool_use_start was already streamed
                # to the frontend but the done-event re-render will clean it up.

            assistant_msg = Message(role="assistant", content=assistant_blocks)
            new_messages.append(assistant_msg)
            api_messages.append({"role": "assistant", "content": assistant_blocks})

            if stop_reason == "max_tokens":
                _LOGGER.warning(
                    "Claude response truncated by max_tokens=%d on turn %d",
                    DEFAULT_MAX_TOKENS,
                    turn,
                )
                await emit({
                    "type": "error",
                    "error": (
                        "Response truncated (max_tokens limit reached). "
                        "Please try again — if this recurs, reduce the size of "
                        "the request or contact support."
                    ),
                })
                break

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


def _session_state_block(store, session_id: str) -> str:
    """Build a [Session state] summary of past proposals for the system prompt.

    Returns "" when there are no proposals yet — no point telling Claude
    about an empty list.
    """
    session = store.get(session_id) if store else None
    if not session or not session.pending_changes:
        return ""
    icon = {"accepted": "✓", "rejected": "✗", "pending": "⏳"}
    lines = ["[Session state — status of past proposals in this chat]"]
    for c in session.pending_changes:
        prefix = icon.get(c.status, "?")
        lines.append(f"{prefix} {c.status}: {c.summary} ({c.kind})")
    return "\n".join(lines)


def _to_api_messages(history: list[Message], hass, session_id: str) -> list[dict[str, Any]]:
    """Convert stored messages to Anthropic-API shape.

    Also deduplicates large tool results: if get_dashboard is called multiple
    times for the same url_path, only the most recent full-config result is
    kept — earlier ones are replaced with a stub to reduce context size.
    """
    import json as _json

    out = []
    for m in history:
        content = m.content
        if any(b.get("type") == "image_ref" for b in content):
            content = to_api_content(hass, session_id, content)
        out.append({"role": m.role, "content": content})

    # Build tool_use_id → tool_name map from all assistant messages.
    id_to_name: dict[str, str] = {}
    for entry in out:
        if entry["role"] == "assistant":
            for b in entry["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    id_to_name[b["id"]] = b["name"]

    # Find all get_dashboard (full-config) tool_result positions keyed by url_path.
    # Structure: url_path -> list of (out_index, block_index)
    dashboard_positions: dict[str, list[tuple[int, int]]] = {}
    for i, entry in enumerate(out):
        if entry["role"] != "user":
            continue
        for j, b in enumerate(entry["content"]):
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            if id_to_name.get(b.get("tool_use_id", "")) != "get_dashboard":
                continue
            try:
                parsed = _json.loads(b.get("content", "{}"))
            except (_json.JSONDecodeError, TypeError):
                continue
            if "config" not in parsed:  # skip summary=true results
                continue
            url_path = parsed.get("url_path", "")
            dashboard_positions.setdefault(url_path, []).append((i, j))

    # Replace all but the last occurrence with a lightweight stub.
    for positions in dashboard_positions.values():
        for (i, j) in positions[:-1]:
            # Build new content list and entry to avoid mutating shared Message objects.
            old_entry = out[i]
            new_blocks = list(old_entry["content"])
            new_blocks[j] = {**new_blocks[j], "content": '{"note":"earlier fetch superseded"}'}
            out[i] = {"role": old_entry["role"], "content": new_blocks}

    return out


def _stringify_tool_result(result: Any) -> str:
    import json

    return json.dumps(result, default=str)
