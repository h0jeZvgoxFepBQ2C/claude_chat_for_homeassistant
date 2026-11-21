"""Claude Code CLI-backed chat client for Claude subscription accounts.

Implements the same interface as `ClaudeClient` (`default_model`, `list_models`,
`summarize_title`, `stream_chat`) by driving the official `claude` CLI over its
bidirectional `stream-json` + `control_request` SDK protocol.

All Home Assistant tools from `tools.py` are exposed via an in-process SDK MCP
server (`--mcp-config '{"mcpServers":{"homeassistant":{"type":"sdk",...}}}'`)
with all built-in coding tools disabled (`--tools ""`), so Claude can only call
our scoped Home Assistant tools and every write remains staged for approval.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from .claude_cli import build_cli_env
from .claude_client import (
    AVAILABLE_MODELS,
    MODELS_CACHE_TTL,
    SYSTEM_PROMPT,
    TITLE_PROMPT,
    EventEmitter,
    _session_state_block,
    _stringify_tool_result,
)
from .const import MAX_TURNS_PER_REQUEST
from .media import to_api_content
from .storage import Message
from .tools import TOOL_DEFINITIONS, ToolRegistry

_LOGGER = logging.getLogger(__name__)

MCP_SERVER_NAME = "homeassistant"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# 10 MiB line buffer for asyncio subprocess stdout — Lovelace dashboards in
# tool results or initialize payloads can easily exceed asyncio's 64 KiB default.
_STREAM_BUFFER_LIMIT = 10 * 1024 * 1024

_CLI_TOOL_GUIDANCE = (
    "\n- Your Home Assistant tools are provided through the `homeassistant` "
    "MCP server (`mcp__homeassistant__<tool_name>`). Call them directly "
    "whenever you need to inspect or propose changes to Home Assistant."
)


def _strip_mcp_prefix(name: str) -> str:
    if name.startswith(MCP_TOOL_PREFIX):
        return name[len(MCP_TOOL_PREFIX) :]
    return name


def _mcp_tool_definitions() -> list[dict[str, Any]]:
    """Convert TOOL_DEFINITIONS (Anthropic API shape) to MCP tools/list shape."""
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "inputSchema": t.get("input_schema", {"type": "object", "properties": {}}),
        }
        for t in TOOL_DEFINITIONS
    ]


_MODEL_ID_VERSION_RE = re.compile(
    r"^claude-([a-z]+)-(\d{1,2})(?:-(\d{1,2}))?(?:-\d{8}|$|-)", re.IGNORECASE
)
_DESC_VERSION_RE = re.compile(
    r"\b([A-Za-z]+)\s+(\d+(?:\.\d+)?)\b"
)


def _format_model_display_name(item: dict[str, Any], model_id: str) -> str:
    """Ensure model display names include their version (e.g. 'Sonnet 4.6').

    Claude Code's initialize payload sometimes returns bare family names like
    'Haiku', 'Sonnet', 'Opus' in `displayName`, while the concrete version is
    in `resolvedModel` (e.g. 'claude-haiku-4-5-20251001') or `description`
    (e.g. 'Sonnet 4.6 · Most efficient for everyday tasks').
    """
    display = str(item.get("displayName") or "").strip()
    if display and any(ch.isdigit() for ch in display):
        return display

    # 1. Derive from concrete model ID (e.g. claude-haiku-4-5-20251001 -> Haiku 4.5).
    match = _MODEL_ID_VERSION_RE.match(model_id)
    if match:
        family, major, minor = match.group(1), match.group(2), match.group(3)
        version = f"{major}.{minor}" if minor else major
        return f"{family.capitalize()} {version}"

    # 2. Fall back to version mentioned in description (e.g. "Opus 4.6 · ...").
    desc = str(item.get("description") or "")
    for dmatch in _DESC_VERSION_RE.finditer(desc):
        word, ver = dmatch.group(1), dmatch.group(2)
        if not display or word.lower() == display.lower():
            return f"{word.capitalize()} {ver}"

    return display or model_id


def _parse_models_from_init(init_resp: dict[str, Any]) -> list[dict[str, str]]:
    """Extract [{id, name}, ...] from the CLI initialize control response."""
    raw_models = init_resp.get("models")
    if not isinstance(raw_models, list):
        return []

    # Collect family -> versioned label hints from all entries (including
    # "default", which often names the concrete default model like "Opus 4.6").
    family_hints: dict[str, str] = {}
    explicit_values: set[str] = set()
    explicit_resolved: set[str] = set()
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "")
        resolved = str(item.get("resolvedModel") or "")
        if value and value != "default":
            explicit_values.add(value.lower())
        if resolved and value != "default":
            explicit_resolved.add(resolved)
        hint_item = {**item, "displayName": ""} if value == "default" else item
        label = _format_model_display_name(hint_item, resolved or value)
        if any(ch.isdigit() for ch in label):
            first_word = label.split()[0].lower()
            family_hints.setdefault(first_word, label)

    seen: set[str] = set()
    models: list[dict[str, str]] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        resolved = item.get("resolvedModel")
        if value == "default":
            # Skip "default" if its concrete model/family is already listed separately.
            if resolved and resolved in explicit_resolved:
                continue
            default_label = _format_model_display_name(
                {**item, "displayName": ""}, str(resolved or "")
            )
            default_family = default_label.split()[0].lower() if default_label else ""
            if default_family and default_family in explicit_values:
                continue
            model_id = resolved or value
            if not isinstance(model_id, str) or not model_id or model_id in seen:
                continue
            seen.add(model_id)
            models.append(
                {
                    "id": model_id,
                    "name": default_label or str(item.get("displayName") or model_id),
                }
            )
            continue

        model_id = resolved or value
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        name = _format_model_display_name(item, model_id)
        if not any(ch.isdigit() for ch in name):
            hint = family_hints.get(name.lower())
            if hint:
                name = hint
        models.append({"id": model_id, "name": name})
    return models


def _format_prior_history_context(history: list[Message]) -> str:
    """Compact transcript of prior messages when starting a fresh CLI session.

    Used only when a session already has messages from API-key mode (no
    `cli_session_id` yet) or when `--resume` fails because the CLI transcript
    on disk was cleared.
    """
    lines: list[str] = [
        "[Prior conversation context — continue seamlessly from here]"
    ]
    for msg in history:
        role = "User" if msg.role == "user" else "Assistant"
        parts: list[str] = []
        for block in msg.content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif btype == "tool_use":
                parts.append(f"[Called tool {block.get('name', '?')}]")
            elif btype == "tool_result":
                snippet = str(block.get("content", ""))[:400]
                parts.append(f"[Tool result: {snippet}]")
            elif btype == "image_ref":
                parts.append("[Attached image]")
        if parts:
            lines.append(f"{role}: {' '.join(parts)}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


class ClaudeCliClient:
    """Chat client backed by the official Claude Code CLI subprocess."""

    _models_cache: list[dict[str, str]] | None = None
    _models_cache_at: float = 0.0

    def __init__(
        self,
        cli_path: str,
        model: str,
        tools: ToolRegistry,
        *,
        config_dir: str | Path | None = None,
        oauth_token: str | None = None,
    ) -> None:
        self._cli_path = cli_path
        self._default_model = model
        self._tools = tools
        self._config_dir = str(config_dir) if config_dir else None
        self._oauth_token = oauth_token
        self._models_cache: list[dict[str, str]] | None = None
        self._models_cache_at: float = 0.0

    @property
    def default_model(self) -> str:
        return self._default_model

    def _env(self) -> dict[str, str]:
        return build_cli_env(
            config_dir=self._config_dir,
            oauth_token=self._oauth_token,
        )

    def _build_stream_cmd(
        self,
        *,
        model: str | None,
        system_prompt: str,
        resume_id: str | None,
        persist_session: bool,
        include_mcp: bool = True,
    ) -> list[str]:
        cmd = [
            self._cli_path,
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-chrome",
            "--max-turns",
            str(MAX_TURNS_PER_REQUEST),
            "--system-prompt",
            system_prompt,
            "--system-prompt-snapshot",
            "off",
        ]
        if include_mcp:
            mcp_cfg = {
                "mcpServers": {
                    MCP_SERVER_NAME: {
                        "type": "sdk",
                        "name": MCP_SERVER_NAME,
                    }
                }
            }
            cmd.extend(
                [
                    "--strict-mcp-config",
                    "--mcp-config",
                    json.dumps(mcp_cfg),
                    "--allowedTools",
                    f"{MCP_TOOL_PREFIX}*",
                ]
            )
        if model:
            cmd.extend(["--model", model])
        if resume_id:
            cmd.append(f"--resume={resume_id}")
        elif not persist_session:
            cmd.append("--no-session-persistence")
        return cmd

    async def list_models(self) -> list[dict[str, str]]:
        """Fetch live models from the CLI's initialize handshake, cached 1h."""
        now = time.time()
        if self._models_cache and now - self._models_cache_at < MODELS_CACHE_TTL:
            return self._models_cache

        cmd = self._build_stream_cmd(
            model=None,
            system_prompt="",
            resume_id=None,
            persist_session=False,
            include_mcp=False,
        )
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
                limit=_STREAM_BUFFER_LIMIT,
            )
            assert proc.stdin is not None and proc.stdout is not None
            init_req = {
                "type": "control_request",
                "request_id": "req_models",
                "request": {"subtype": "initialize"},
            }
            proc.stdin.write((json.dumps(init_req) + "\n").encode("utf-8"))
            await proc.stdin.drain()

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                raw_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=remaining
                )
                if not raw_line:
                    break
                try:
                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "control_response":
                    resp = (obj.get("response") or {}).get("response") or {}
                    models = _parse_models_from_init(resp)
                    if models:
                        self._models_cache = models
                        self._models_cache_at = time.time()
                        return models
                    break
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Claude CLI model discovery unavailable: %s", err)
        finally:
            await _terminate_process(proc)

        return AVAILABLE_MODELS

    async def summarize_title(self, user_text: str) -> str:
        """Quick non-streaming CLI call to generate a short chat title."""
        if not user_text.strip():
            return ""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path,
                "-p",
                "--output-format",
                "json",
                "--model",
                "haiku",
                "--no-session-persistence",
                "--tools",
                "",
                "--setting-sources",
                "",
                "--disable-slash-commands",
                "--no-chrome",
                "--max-turns",
                "1",
                "--system-prompt",
                TITLE_PROMPT,
                user_text[:500],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
                limit=_STREAM_BUFFER_LIMIT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                return ""
            raw = stdout.decode("utf-8", errors="replace").strip()
            data = json.loads(raw)
            if isinstance(data, dict) and not data.get("is_error"):
                text = str(data.get("result") or "").strip()
                return text.strip("\"'.,!?")[:60]
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Title summarization via Claude CLI failed")
        finally:
            if proc is not None and proc.returncode is None:
                await _terminate_process(proc)
        return ""

    async def stream_chat(
        self,
        history: list[Message],
        session_id: str,
        emit: EventEmitter,
        model: str | None = None,
    ) -> list[Message]:
        """Run a streaming turn via the Claude Code CLI with in-process HA tools."""
        active_model = model or self._default_model
        session = self._tools.store.get(session_id) if self._tools.store else None
        cli_session_id = session.cli_session_id if session else None

        system_prompt = SYSTEM_PROMPT + _CLI_TOOL_GUIDANCE
        state_block = _session_state_block(self._tools.store, session_id)
        if state_block:
            system_prompt = f"{system_prompt}\n\n{state_block}"

        # Build the user content blocks for the latest user message (with any
        # attached images inlined as base64 image blocks).
        latest_content = _latest_user_content(history, self._tools.hass, session_id)
        prior_history = history[:-1] if len(history) > 1 else []

        if cli_session_id:
            # Try resuming the existing CLI session. If it fails before any
            # streaming output (e.g. CLI transcript pruned), fall back cleanly
            # to a fresh session with prior history context.
            new_messages, resumed_ok, emitted_any = await self._run_cli_turn(
                user_content=latest_content,
                session_id=session_id,
                emit=emit,
                model=active_model,
                system_prompt=system_prompt,
                resume_id=cli_session_id,
                suppress_pre_stream_error=True,
            )
            if resumed_ok or emitted_any:
                return new_messages
            _LOGGER.info(
                "Claude CLI resume failed for session %s; starting fresh CLI session",
                session_id,
            )
            if self._tools.store and session:
                await self._tools.store.set_cli_session_id(session_id, None)

        # Fresh CLI session — prepend prior conversation summary if migrating
        # an existing chat that already has history.
        send_content = list(latest_content)
        if prior_history:
            context = _format_prior_history_context(prior_history)
            if context:
                send_content = [{"type": "text", "text": context}, *send_content]

        new_messages, _, _ = await self._run_cli_turn(
            user_content=send_content,
            session_id=session_id,
            emit=emit,
            model=active_model,
            system_prompt=system_prompt,
            resume_id=None,
            suppress_pre_stream_error=False,
        )
        return new_messages

    async def _run_cli_turn(
        self,
        *,
        user_content: list[dict[str, Any]],
        session_id: str,
        emit: EventEmitter,
        model: str,
        system_prompt: str,
        resume_id: str | None,
        suppress_pre_stream_error: bool,
    ) -> tuple[list[Message], bool, bool]:
        """Execute one interactive streaming turn against a Claude CLI subprocess.

        Returns `(new_messages, succeeded, emitted_any_stream_event)`.
        """
        cmd = self._build_stream_cmd(
            model=model,
            system_prompt=system_prompt,
            resume_id=resume_id,
            persist_session=True,
            include_mcp=True,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
            limit=_STREAM_BUFFER_LIMIT,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        stdin_lock = asyncio.Lock()
        bg_tasks: set[asyncio.Task[Any]] = set()
        new_messages: list[Message] = []
        pending_tool_ids: dict[str, deque[str]] = {}
        seen_tool_ids: set[str] = set()
        turn_tool_results: list[dict[str, Any]] = []
        emitted_any = False
        captured_cli_session_id: str | None = resume_id
        succeeded = False
        result_received = False

        async def _write_json(payload: dict[str, Any]) -> None:
            async with stdin_lock:
                if proc.stdin is None or proc.stdin.is_closing():
                    return
                proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                await proc.stdin.drain()

        def _register_tool_id(clean_name: str, tool_use_id: str) -> None:
            if tool_use_id in seen_tool_ids:
                return
            seen_tool_ids.add(tool_use_id)
            pending_tool_ids.setdefault(clean_name, deque()).append(tool_use_id)

        def _pop_tool_id(clean_name: str) -> str:
            q = pending_tool_ids.get(clean_name)
            if q:
                return q.popleft()
            return f"toolu_{uuid.uuid4().hex[:16]}"

        def _flush_tool_results() -> None:
            if turn_tool_results:
                new_messages.append(
                    Message(role="user", content=list(turn_tool_results))
                )
                turn_tool_results.clear()

        async def _handle_mcp_message(
            msg: dict[str, Any],
        ) -> dict[str, Any]:
            nonlocal emitted_any
            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {}

            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": MCP_SERVER_NAME,
                            "version": "1.0.0",
                        },
                    },
                }

            if method == "notifications/initialized" or req_id is None:
                return {"jsonrpc": "2.0", "result": {}}

            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            if method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": _mcp_tool_definitions()},
                }

            if method == "tools/call":
                raw_name = str(params.get("name") or "")
                clean_name = _strip_mcp_prefix(raw_name)
                arguments = params.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {}
                tool_use_id = _pop_tool_id(clean_name)

                result = await self._tools.call(
                    clean_name, arguments, session_id, tool_use_id
                )
                emitted_any = True
                await emit(
                    {
                        "type": "tool_result",
                        "id": tool_use_id,
                        "name": clean_name,
                        "result": result,
                    }
                )
                result_text = _stringify_tool_result(result)
                turn_tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_text,
                    }
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": result_text}],
                        "isError": False,
                    },
                }

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }

        async def _handle_control_request(envelope: dict[str, Any]) -> None:
            request_id = envelope.get("request_id")
            request = envelope.get("request") or {}
            subtype = request.get("subtype")
            try:
                if subtype == "mcp_message":
                    server_name = request.get("server_name")
                    mcp_message = request.get("message")
                    if server_name != MCP_SERVER_NAME or not isinstance(
                        mcp_message, dict
                    ):
                        mcp_response: dict[str, Any] = {
                            "jsonrpc": "2.0",
                            "id": (
                                mcp_message.get("id")
                                if isinstance(mcp_message, dict)
                                else None
                            ),
                            "error": {
                                "code": -32601,
                                "message": f"Unknown MCP server: {server_name}",
                            },
                        }
                    else:
                        mcp_response = await _handle_mcp_message(mcp_message)
                    response_data: dict[str, Any] = {"mcp_response": mcp_response}
                elif subtype == "can_use_tool":
                    tool_name = str(request.get("tool_name") or "")
                    if tool_name.startswith(MCP_TOOL_PREFIX):
                        response_data = {
                            "behavior": "allow",
                            "updatedInput": request.get("input") or {},
                        }
                    else:
                        response_data = {
                            "behavior": "deny",
                            "message": "Only Home Assistant tools are permitted.",
                        }
                else:
                    raise ValueError(f"Unsupported control request: {subtype}")

                await _write_json(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": request_id,
                            "response": response_data,
                        },
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Error handling CLI control_request %s", subtype)
                await _write_json(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": request_id,
                            "error": str(err),
                        },
                    }
                )

        def _spawn_control_handler(envelope: dict[str, Any]) -> None:
            task = asyncio.create_task(_handle_control_request(envelope))
            bg_tasks.add(task)
            task.add_done_callback(bg_tasks.discard)

        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

        stderr_task = asyncio.create_task(_drain_stderr())

        try:
            # 1. Handshake so the CLI initializes SDK MCP tools and reports models.
            init_req_id = f"req_init_{uuid.uuid4().hex[:8]}"
            await _write_json(
                {
                    "type": "control_request",
                    "request_id": init_req_id,
                    "request": {"subtype": "initialize"},
                }
            )

            init_done = False
            while not init_done:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
                if not raw_line:
                    break
                try:
                    msg = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "control_request":
                    _spawn_control_handler(msg)
                elif mtype == "control_response":
                    resp_obj = msg.get("response") or {}
                    if resp_obj.get("request_id") == init_req_id:
                        if resp_obj.get("subtype") == "error":
                            raise RuntimeError(
                                str(resp_obj.get("error") or "CLI initialize failed")
                            )
                        init_data = resp_obj.get("response") or {}
                        models = _parse_models_from_init(init_data)
                        if models:
                            self._models_cache = models
                            self._models_cache_at = time.time()
                        init_done = True

            if not init_done:
                err_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
                if not suppress_pre_stream_error:
                    await emit(
                        {
                            "type": "error",
                            "error": err_text or "Claude CLI exited before initialization.",
                        }
                    )
                return new_messages, False, emitted_any

            # 2. Send the user turn (text and/or base64 image blocks).
            await _write_json(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": user_content,
                    },
                    "parent_tool_use_id": None,
                    "session_id": captured_cli_session_id or "default",
                }
            )

            # 3. Stream events until `result` arrives.
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                try:
                    msg = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue

                if msg.get("session_id"):
                    captured_cli_session_id = str(msg["session_id"])

                mtype = msg.get("type")

                if mtype == "control_request":
                    _spawn_control_handler(msg)
                    continue

                if mtype == "stream_event":
                    ev = msg.get("event") or {}
                    etype = ev.get("type")
                    if etype == "content_block_start":
                        block = ev.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            clean_name = _strip_mcp_prefix(str(block.get("name") or ""))
                            tool_id = str(block.get("id") or f"toolu_{uuid.uuid4().hex[:16]}")
                            _register_tool_id(clean_name, tool_id)
                            emitted_any = True
                            await emit(
                                {
                                    "type": "tool_use_start",
                                    "id": tool_id,
                                    "name": clean_name,
                                }
                            )
                    elif etype == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text") or ""
                            if text:
                                emitted_any = True
                                await emit({"type": "text_delta", "text": text})
                    continue

                if mtype == "assistant":
                    _flush_tool_results()
                    raw_blocks = (msg.get("message") or {}).get("content") or []
                    normalized: list[dict[str, Any]] = []
                    for block in raw_blocks:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            normalized.append(
                                {"type": "text", "text": str(block.get("text") or "")}
                            )
                        elif btype == "tool_use":
                            clean_name = _strip_mcp_prefix(str(block.get("name") or ""))
                            tool_id = str(block.get("id") or f"toolu_{uuid.uuid4().hex[:16]}")
                            _register_tool_id(clean_name, tool_id)
                            normalized.append(
                                {
                                    "type": "tool_use",
                                    "id": tool_id,
                                    "name": clean_name,
                                    "input": block.get("input") or {},
                                }
                            )
                    if normalized:
                        new_messages.append(Message(role="assistant", content=normalized))
                    continue

                if mtype == "user":
                    # CLI echoed the tool_result turn back — flush our recorded
                    # tool results into new_messages to keep history aligned.
                    _flush_tool_results()
                    continue

                if mtype == "result":
                    result_received = True
                    _flush_tool_results()
                    if bg_tasks:
                        await asyncio.gather(*bg_tasks, return_exceptions=True)

                    if msg.get("is_error"):
                        err_detail = str(
                            msg.get("result")
                            or (msg.get("errors") or [""])[0]
                            or "Claude returned an error."
                        )
                        if not (suppress_pre_stream_error and not emitted_any):
                            await emit({"type": "error", "error": err_detail})
                        succeeded = False
                    else:
                        if (
                            captured_cli_session_id
                            and self._tools.store is not None
                            and self._tools.store.get(session_id) is not None
                        ):
                            await self._tools.store.set_cli_session_id(
                                session_id, captured_cli_session_id
                            )
                        await emit({"type": "turn_complete"})
                        succeeded = True
                    break

            if not result_received:
                _flush_tool_results()
                await stderr_task
                err_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
                if not (suppress_pre_stream_error and not emitted_any):
                    await emit(
                        {
                            "type": "error",
                            "error": err_text
                            or "Claude CLI stream ended unexpectedly.",
                        }
                    )
        finally:
            stderr_task.cancel()
            for task in list(bg_tasks):
                task.cancel()
            if bg_tasks:
                await asyncio.gather(*bg_tasks, return_exceptions=True)
            await _terminate_process(proc)

        return new_messages, succeeded, emitted_any


def _latest_user_content(
    history: list[Message], hass: Any, session_id: str
) -> list[dict[str, Any]]:
    """Return the latest user message's content blocks with images inlined."""
    if not history:
        return [{"type": "text", "text": ""}]
    last = history[-1]
    content = last.content
    if any(isinstance(b, dict) and b.get("type") == "image_ref" for b in content):
        content = to_api_content(hass, session_id, content)
    return list(content)


async def _terminate_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None:
        return
    if proc.stdin is not None and not proc.stdin.is_closing():
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except TimeoutError:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
