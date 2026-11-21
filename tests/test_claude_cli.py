"""Tests for Claude Code CLI management, auth helpers, and ClaudeCliClient."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.claude_chat.claude_cli import (
    ClaudeCliAuthError,
    ClaudeCliDownloadError,
    ClaudeLoginSession,
    async_check_auth_status,
    async_ensure_cli_binary,
    async_validate_cli_auth,
    build_cli_env,
    detect_platform,
    extract_auth_url,
    strip_ansi,
)
from custom_components.claude_chat.claude_cli_client import (
    MCP_TOOL_PREFIX,
    ClaudeCliClient,
    _parse_models_from_init,
)
from custom_components.claude_chat.storage import Message, SessionStore
from custom_components.claude_chat.tools import ToolRegistry


def test_strip_ansi_and_extract_auth_url():
    raw = (
        "Opening browser to sign in…\r\n"
        "If the browser didn't open, visit: "
        "\x1b]8;;https://claude.com/cai/oauth/authorize?code=true&state=xyz\x07"
        "https://claude.com/cai/oauth/authorize?code=true&state=xyz"
        "\x1b]8;;\x07\r\n"
        "Paste code here if prompted > "
    )
    assert "Opening browser" in strip_ansi(raw)
    assert (
        extract_auth_url(raw)
        == "https://claude.com/cai/oauth/authorize?code=true&state=xyz"
    )
    # Truncated mid-chunk URL (no trailing whitespace yet) must not match.
    assert (
        extract_auth_url("visit: https://claude.com/cai/oauth/authorize?code=tr")
        is None
    )


def test_build_cli_env_strips_api_key_and_sets_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-should-be-stripped")
    cfg_dir = tmp_path / "claude_cfg"

    env = build_cli_env(config_dir=cfg_dir, oauth_token="sk-ant-oat01-abc", headless=True)
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg_dir)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"
    assert env["BROWSER"] == "false"
    assert env["SSH_CONNECTION"]
    assert cfg_dir.is_dir()


@pytest.mark.parametrize(
    ("sys_name", "machine", "is_musl", "expected"),
    [
        ("Linux", "x86_64", True, "linux-x64-musl"),
        ("Linux", "aarch64", True, "linux-arm64-musl"),
        ("Linux", "x86_64", False, "linux-x64"),
        ("Linux", "aarch64", False, "linux-arm64"),
        ("Darwin", "arm64", False, "darwin-arm64"),
    ],
)
def test_detect_platform(sys_name, machine, is_musl, expected):
    with patch("platform.system", return_value=sys_name), patch(
        "platform.machine", return_value=machine
    ), patch(
        "custom_components.claude_chat.claude_cli._is_musl_linux",
        return_value=is_musl,
    ):
        assert detect_platform() == expected


def test_parse_models_from_init():
    init_resp = {
        "models": [
            {"value": "default", "resolvedModel": "claude-opus-5-5", "displayName": "Default"},
            {"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "Opus 5.5"},
            {"value": "sonnet", "resolvedModel": "claude-sonnet-5-5", "displayName": "Sonnet 5.5"},
            # Duplicate resolvedModel should be deduped.
            {"value": "claude-opus-5-5", "resolvedModel": "claude-opus-5-5", "displayName": "Opus 5.5 dup"},
        ]
    }
    assert _parse_models_from_init(init_resp) == [
        {"id": "claude-opus-5-5", "name": "Opus 5.5"},
        {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"},
    ]


def test_parse_models_from_init_adds_versions_when_display_name_is_bare():
    init_resp = {
        "models": [
            {
                "value": "haiku",
                "resolvedModel": "claude-haiku-4-5-20251001",
                "displayName": "Haiku",
                "description": "Fastest for quick answers",
            },
            {
                "value": "sonnet",
                "displayName": "Sonnet",
                "description": "Sonnet 4.6 · Most efficient for everyday tasks",
            },
            {
                "value": "opus",
                "resolvedModel": "claude-opus-4-7",
                "displayName": "Opus",
                "description": "Best for complex tasks",
            },
            {
                "value": "claude-sonnet-5",
                "displayName": "Sonnet",
            },
        ]
    }
    assert _parse_models_from_init(init_resp) == [
        {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5"},
        {"id": "sonnet", "name": "Sonnet 4.6"},
        {"id": "claude-opus-4-7", "name": "Opus 4.7"},
        {"id": "claude-sonnet-5", "name": "Sonnet 5"},
    ]

    # When "default" carries the version hint for a bare "opus" entry (or is the only Opus entry):
    init_with_default_hint = {
        "models": [
            {
                "value": "default",
                "displayName": "Default (recommended)",
                "description": "Opus 4.6 · Most capable for complex work",
            },
            {
                "value": "opus",
                "displayName": "Opus",
                "description": "Most capable model",
            },
            {
                "value": "sonnet",
                "displayName": "Sonnet",
                "description": "Sonnet 4.6 · Everyday tasks",
            },
        ]
    }
    assert _parse_models_from_init(init_with_default_hint) == [
        {"id": "opus", "name": "Opus 4.6"},
        {"id": "sonnet", "name": "Sonnet 4.6"},
    ]


class _FakeHttpResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def text(self) -> str:
        return self._body.decode("utf-8")

    async def json(self, content_type=None):
        return json.loads(self._body.decode("utf-8"))

    async def iter_chunked(self, size: int):
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]


class _FakeHttpSession:
    def __init__(self, routes: dict[str, bytes]):
        self._routes = routes

    def get(self, url: str, timeout=None):
        if url not in self._routes:
            return _FakeHttpResp(b"not found", status=404)
        return _FakeHttpResp(self._routes[url])


async def test_ensure_cli_binary_downloads_and_verifies_sha256(hass):
    binary_bytes = b"#!/bin/sh\necho '2.1.330 (Claude Code)'\n"
    sha = hashlib.sha256(binary_bytes).hexdigest()
    manifest = {"platforms": {"linux-x64-musl": {"checksum": sha, "size": len(binary_bytes)}}}
    base = "https://downloads.claude.ai/claude-code-releases"
    fake_http = _FakeHttpSession(
        {
            f"{base}/latest": b"2.1.330\n",
            f"{base}/2.1.330/manifest.json": json.dumps(manifest).encode(),
            f"{base}/2.1.330/linux-x64-musl/claude": binary_bytes,
        }
    )

    with patch(
        "custom_components.claude_chat.claude_cli.find_cli_binary", return_value=None
    ), patch(
        "custom_components.claude_chat.claude_cli.detect_platform",
        return_value="linux-x64-musl",
    ):
        installed = await async_ensure_cli_binary(hass, session=fake_http)

    assert Path(installed).is_file()
    assert os.access(installed, os.X_OK)


async def test_ensure_cli_binary_rejects_bad_checksum(hass):
    binary_bytes = b"corrupted"
    bad_sha = "0" * 64
    manifest = {"platforms": {"linux-x64": {"checksum": bad_sha}}}
    base = "https://downloads.claude.ai/claude-code-releases"
    fake_http = _FakeHttpSession(
        {
            f"{base}/latest": b"2.1.330",
            f"{base}/2.1.330/manifest.json": json.dumps(manifest).encode(),
            f"{base}/2.1.330/linux-x64/claude": binary_bytes,
        }
    )

    with patch(
        "custom_components.claude_chat.claude_cli.find_cli_binary", return_value=None
    ), patch(
        "custom_components.claude_chat.claude_cli.detect_platform",
        return_value="linux-x64",
    ):
        with pytest.raises(ClaudeCliDownloadError, match="Checksum mismatch"):
            await async_ensure_cli_binary(hass, session=fake_http)


def _write_fake_cli_script(path: Path, body: str) -> str:
    script = f"#!{sys.executable}\n{body}"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_login_session_pty_flow_and_auth_status(tmp_path):
    """End-to-end test of ClaudeLoginSession PTY interaction against a fake CLI."""
    cfg_dir = tmp_path / "cfg"
    cli_path = _write_fake_cli_script(
        tmp_path / "fake_claude",
        """
import json, os, sys
from pathlib import Path

cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
marker = cfg / "logged_in.json"

if sys.argv[1:3] == ["auth", "status"]:
    if marker.exists():
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "u@example.com"}))
        sys.exit(0)
    print(json.dumps({"loggedIn": False, "authMethod": "none"}))
    sys.exit(1)

if sys.argv[1:3] == ["auth", "login"]:
    sys.stdout.write("Opening browser to sign in...\\n")
    sys.stdout.write("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=s123\\n")
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "valid-code#s123":
        marker.write_text("ok")
        sys.stdout.write("Login successful!\\n")
        sys.exit(0)
    sys.stdout.write("Invalid authorization code\\n")
    sys.exit(1)
""",
    )

    status_before = await async_check_auth_status(cli_path, config_dir=cfg_dir)
    assert status_before["loggedIn"] is False

    login = ClaudeLoginSession(cli_path, cfg_dir)
    url = await login.start()
    assert url == "https://claude.com/cai/oauth/authorize?code=true&state=s123"

    ok, err = await login.submit_code("valid-code#s123")
    assert ok is True
    assert err == ""

    status_after = await async_check_auth_status(cli_path, config_dir=cfg_dir)
    assert status_after["loggedIn"] is True
    assert status_after["email"] == "u@example.com"


async def test_validate_cli_auth_error_detection(tmp_path):
    cli_path = _write_fake_cli_script(
        tmp_path / "fake_claude_auth",
        """
import json, os, sys
tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
if tok == "sk-ant-oat01-valid":
    print(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
    sys.exit(0)
print(json.dumps({"type": "result", "is_error": True, "result": "Invalid authentication token"}))
sys.exit(1)
""",
    )
    await async_validate_cli_auth(cli_path, oauth_token="sk-ant-oat01-valid")
    with pytest.raises(ClaudeCliAuthError):
        await async_validate_cli_auth(cli_path, oauth_token="sk-ant-oat01-bad")


async def test_cli_client_stream_chat_with_in_process_mcp_tools(hass, tmp_path):
    """Full stream-json + SDK MCP tool call round-trip via a fake CLI subprocess."""
    hass.states.async_set("sensor.living_room_temp", "21.5", {"unit_of_measurement": "°C"})
    cli_path = _write_fake_cli_script(
        tmp_path / "fake_claude_stream",
        f"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def recv():
    line = sys.stdin.readline()
    return json.loads(line) if line else None

# 1. Wait for initialize control_request
init_req = recv()
assert init_req["type"] == "control_request"
assert init_req["request"]["subtype"] == "initialize"

# Ask host for MCP initialize + tools/list
send({{
    "type": "control_request",
    "request_id": "mcp_1",
    "request": {{
        "subtype": "mcp_message",
        "server_name": "homeassistant",
        "message": {{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {{}}}}
    }}
}})
mcp_init_resp = recv()
assert mcp_init_resp["response"]["response"]["mcp_response"]["result"]["serverInfo"]["name"] == "homeassistant"

send({{
    "type": "control_request",
    "request_id": "mcp_2",
    "request": {{
        "subtype": "mcp_message",
        "server_name": "homeassistant",
        "message": {{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {{}}}}
    }}
}})
tools_resp = recv()
tool_names = [t["name"] for t in tools_resp["response"]["response"]["mcp_response"]["result"]["tools"]]
assert "list_entities" in tool_names

# Complete initialize handshake with models list
send({{
    "type": "control_response",
    "response": {{
        "subtype": "success",
        "request_id": init_req["request_id"],
        "response": {{
            "models": [
                {{"value": "sonnet", "resolvedModel": "claude-sonnet-5-5", "displayName": "Sonnet 5.5"}},
                {{"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "Opus 5.5"}}
            ]
        }}
    }}
}})

# 2. Read user message
user_msg = recv()
assert user_msg["type"] == "user"

# 3. Stream text + MCP tool_use
send({{"type": "system", "subtype": "init", "session_id": "cli-sess-42"}})
send({{"type": "stream_event", "session_id": "cli-sess-42", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": "Checking sensors..."}}}}}})
send({{"type": "stream_event", "session_id": "cli-sess-42", "event": {{"type": "content_block_start", "content_block": {{"type": "tool_use", "id": "toolu_ha_1", "name": "{MCP_TOOL_PREFIX}list_entities"}}}}}})
send({{
    "type": "assistant",
    "session_id": "cli-sess-42",
    "message": {{
        "role": "assistant",
        "content": [
            {{"type": "text", "text": "Checking sensors..."}},
            {{"type": "tool_use", "id": "toolu_ha_1", "name": "{MCP_TOOL_PREFIX}list_entities", "input": {{"domain": "sensor"}}}}
        ]
    }}
}})

# 4. Invoke the in-process HA MCP tool
send({{
    "type": "control_request",
    "request_id": "mcp_call_1",
    "request": {{
        "subtype": "mcp_message",
        "server_name": "homeassistant",
        "message": {{
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {{"name": "list_entities", "arguments": {{"domain": "sensor"}}}}
        }}
    }}
}})
call_resp = recv()
tool_payload = json.loads(call_resp["response"]["response"]["mcp_response"]["result"]["content"][0]["text"])
assert any(e["entity_id"] == "sensor.living_room_temp" for e in tool_payload["entities"])

# 5. Echo tool result turn and emit final text + result
send({{"type": "user", "session_id": "cli-sess-42", "message": {{"role": "user", "content": []}}}})
send({{"type": "stream_event", "session_id": "cli-sess-42", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": "It is 21.5°C."}}}}}})
send({{
    "type": "assistant",
    "session_id": "cli-sess-42",
    "message": {{"role": "assistant", "content": [{{"type": "text", "text": "It is 21.5°C."}}]}}
}})
send({{"type": "result", "subtype": "success", "is_error": False, "session_id": "cli-sess-42", "result": "It is 21.5°C."}})
""",
    )

    store = SessionStore(hass)
    await store.async_load()
    tools = ToolRegistry(hass, store)
    session = await store.create()

    client = ClaudeCliClient(
        cli_path=cli_path,
        model="claude-sonnet-5-5",
        tools=tools,
        config_dir=tmp_path / "cfg",
    )

    events: list[dict] = []

    async def emit(ev: dict) -> None:
        events.append(ev)

    history = [Message(role="user", content=[{"type": "text", "text": "Check temp"}])]
    new_messages = await client.stream_chat(history, session.id, emit)

    # Events match the exact frontend protocol (with mcp__homeassistant__ stripped!).
    assert any(e == {"type": "text_delta", "text": "Checking sensors..."} for e in events)
    assert any(
        e == {"type": "tool_use_start", "id": "toolu_ha_1", "name": "list_entities"}
        for e in events
    )
    tool_res_ev = next(e for e in events if e["type"] == "tool_result")
    assert tool_res_ev["id"] == "toolu_ha_1"
    assert tool_res_ev["name"] == "list_entities"
    assert any(
        ent["entity_id"] == "sensor.living_room_temp"
        for ent in tool_res_ev["result"]["entities"]
    )
    assert any(e == {"type": "text_delta", "text": "It is 21.5°C."} for e in events)
    assert any(e == {"type": "turn_complete"} for e in events)

    # Saved message shape matches ClaudeClient: assistant(tool_use) -> user(tool_result) -> assistant(text).
    assert len(new_messages) == 3
    assert new_messages[0].role == "assistant"
    assert new_messages[0].content[1]["name"] == "list_entities"
    assert new_messages[1].role == "user"
    assert new_messages[1].content[0]["type"] == "tool_result"
    assert new_messages[1].content[0]["tool_use_id"] == "toolu_ha_1"
    assert new_messages[2].role == "assistant"
    assert new_messages[2].content[0]["text"] == "It is 21.5°C."

    # Verify CLI flags work under root in Docker (never bypassPermissions).
    cmd = client._build_stream_cmd(
        model="claude-sonnet-5-5",
        system_prompt="test",
        resume_id=None,
        persist_session=True,
    )
    assert "bypassPermissions" not in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"

    # CLI session ID was persisted for multi-turn --resume, and models were cached from initialize.
    assert store.get_or_raise(session.id).cli_session_id == "cli-sess-42"
    models = await client.list_models()
    assert models == [
        {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"},
        {"id": "claude-opus-5-5", "name": "Opus 5.5"},
    ]


async def test_cli_client_resume_fallback_and_summarize_title(hass, tmp_path):
    """If --resume fails before streaming, client transparently retries a fresh session."""
    cli_path = _write_fake_cli_script(
        tmp_path / "fake_claude_resume",
        """
import json, sys

args = sys.argv[1:]
if "-p" in args:
    print(json.dumps({"type": "result", "is_error": False, "result": "Living room sensors"}))
    sys.exit(0)

resumed = any(a.startswith("--resume=") for a in args)

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def recv():
    line = sys.stdin.readline()
    return json.loads(line) if line else None

init_req = recv()
send({
    "type": "control_response",
    "response": {"subtype": "success", "request_id": init_req["request_id"], "response": {}}
})
user_msg = recv()

if resumed:
    send({"type": "result", "subtype": "error", "is_error": True, "session_id": "stale", "result": "Session not found"})
    sys.exit(1)

# Fresh fallback must receive prior context prepended.
blocks = user_msg["message"]["content"]
assert any("Prior conversation context" in b.get("text", "") for b in blocks)
send({"type": "stream_event", "session_id": "fresh-99", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Recovered!"}}})
send({"type": "assistant", "session_id": "fresh-99", "message": {"role": "assistant", "content": [{"type": "text", "text": "Recovered!"}]}})
send({"type": "result", "subtype": "success", "is_error": False, "session_id": "fresh-99", "result": "Recovered!"})
""",
    )

    store = SessionStore(hass)
    await store.async_load()
    tools = ToolRegistry(hass, store)
    session = await store.create()
    await store.set_cli_session_id(session.id, "stale-session-id")

    client = ClaudeCliClient(
        cli_path=cli_path,
        model="claude-sonnet-5",
        tools=tools,
        config_dir=tmp_path / "cfg",
    )

    events: list[dict] = []

    async def emit(ev: dict) -> None:
        events.append(ev)

    history = [
        Message(role="user", content=[{"type": "text", "text": "Old question"}]),
        Message(role="assistant", content=[{"type": "text", "text": "Old answer"}]),
        Message(role="user", content=[{"type": "text", "text": "Follow up"}]),
    ]
    new_messages = await client.stream_chat(history, session.id, emit)

    assert not any(e["type"] == "error" for e in events)
    assert any(e == {"type": "text_delta", "text": "Recovered!"} for e in events)
    assert any(e == {"type": "turn_complete"} for e in events)
    assert len(new_messages) == 1
    assert store.get_or_raise(session.id).cli_session_id == "fresh-99"

    title = await client.summarize_title("What sensors are in the living room?")
    assert title == "Living room sensors"
