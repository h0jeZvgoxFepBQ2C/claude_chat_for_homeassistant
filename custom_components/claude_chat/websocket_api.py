"""WebSocket API for Claude Chat panel.

Commands:
  claude_chat/list_sessions
  claude_chat/get_session
  claude_chat/create_session
  claude_chat/delete_session
  claude_chat/rename_session
  claude_chat/send_message      (subscription — streams events)
  claude_chat/approve_change
  claude_chat/reject_change
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .claude_client import AVAILABLE_MODELS, ClaudeClient
from .const import DOMAIN
from .storage import Message, SessionStore
from .tools import ToolRegistry, automations_include_configured


def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list_sessions)
    websocket_api.async_register_command(hass, ws_get_session)
    websocket_api.async_register_command(hass, ws_create_session)
    websocket_api.async_register_command(hass, ws_delete_session)
    websocket_api.async_register_command(hass, ws_rename_session)
    websocket_api.async_register_command(hass, ws_send_message)
    websocket_api.async_register_command(hass, ws_approve_change)
    websocket_api.async_register_command(hass, ws_reject_change)
    websocket_api.async_register_command(hass, ws_list_models)
    websocket_api.async_register_command(hass, ws_diagnostics)


def _runtime(hass: HomeAssistant) -> dict[str, Any] | None:
    return hass.data.get(DOMAIN)


def _store(hass: HomeAssistant) -> SessionStore:
    return _runtime(hass)["store"]


def _client(hass: HomeAssistant) -> ClaudeClient:
    return _runtime(hass)["client"]


def _tools(hass: HomeAssistant) -> ToolRegistry:
    return _runtime(hass)["tools"]


def _serialize_session(session) -> dict[str, Any]:
    return {
        "id": session.id,
        "title": session.title,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "messages": [asdict(m) for m in session.messages],
        "pending_changes": [asdict(p) for p in session.pending_changes],
    }


@websocket_api.websocket_command({vol.Required("type"): "claude_chat/list_sessions"})
@websocket_api.async_response
async def ws_list_sessions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    if not _runtime(hass):
        connection.send_error(msg["id"], "not_configured", "Claude Chat not set up")
        return
    connection.send_result(msg["id"], {"sessions": _store(hass).list_sessions()})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "claude_chat/get_session",
        vol.Required("session_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    session = _store(hass).get(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return
    connection.send_result(msg["id"], _serialize_session(session))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "claude_chat/create_session",
        vol.Optional("title", default="New chat"): str,
    }
)
@websocket_api.async_response
async def ws_create_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    session = await _store(hass).create(title=msg["title"])
    connection.send_result(msg["id"], _serialize_session(session))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "claude_chat/delete_session",
        vol.Required("session_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    await _store(hass).delete(msg["session_id"])
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "claude_chat/rename_session",
        vol.Required("session_id"): str,
        vol.Required("title"): str,
    }
)
@websocket_api.async_response
async def ws_rename_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    await _store(hass).rename(msg["session_id"], msg["title"])
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "claude_chat/send_message",
        vol.Required("session_id"): str,
        vol.Required("text"): str,
        vol.Optional("model"): str,
    }
)
@websocket_api.async_response
async def ws_send_message(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Stream-style command. Emits events back as `event` results until done."""
    runtime = _runtime(hass)
    if not runtime:
        connection.send_error(msg["id"], "not_configured", "Claude Chat not set up")
        return

    store = runtime["store"]
    client: ClaudeClient = runtime["client"]
    session = store.get(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return

    is_first_user_message = not any(m.role == "user" for m in session.messages)
    user_text = msg["text"]
    user_message = Message(
        role="user", content=[{"type": "text", "text": user_text}]
    )
    await store.append_message(session.id, user_message)
    connection.send_result(msg["id"], {"streaming": True})

    @callback
    def emit_sync(event: dict[str, Any]) -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], event)
        )

    async def emit(event: dict[str, Any]) -> None:
        emit_sync(event)

    try:
        new_messages = await client.stream_chat(
            history=session.messages,
            session_id=session.id,
            emit=emit,
            model=msg.get("model"),
        )
        for m in new_messages:
            await store.append_message(session.id, m)

        if is_first_user_message:
            title = await client.summarize_title(user_text)
            if title:
                await store.rename(session.id, title)

        refreshed = store.get_or_raise(session.id)
        await emit(
            {
                "type": "done",
                "session": _serialize_session(refreshed),
            }
        )
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001
        await emit({"type": "error", "error": str(err)})


@websocket_api.websocket_command({vol.Required("type"): "claude_chat/list_models"})
@websocket_api.async_response
async def ws_list_models(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    runtime = _runtime(hass)
    default = runtime["client"].default_model if runtime else None
    connection.send_result(
        msg["id"], {"models": AVAILABLE_MODELS, "default": default}
    )


@websocket_api.websocket_command({vol.Required("type"): "claude_chat/diagnostics"})
@websocket_api.async_response
async def ws_diagnostics(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Front-end uses this to render hint banners (e.g. missing config lines)."""
    automation_supported = await hass.async_add_executor_job(
        automations_include_configured, hass
    )
    connection.send_result(
        msg["id"],
        {
            "automation_create_supported": automation_supported,
            "automation_create_hint": (
                None
                if automation_supported
                else (
                    "Automation creation is disabled because your "
                    "configuration.yaml is missing this line:\n"
                    "    automation: !include automations.yaml\n"
                    "Add it and restart Home Assistant."
                )
            ),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "claude_chat/approve_change",
        vol.Required("session_id"): str,
        vol.Required("change_id"): str,
    }
)
@websocket_api.async_response
async def ws_approve_change(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    store = _store(hass)
    tools = _tools(hass)
    session = store.get(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Session not found")
        return
    change = next(
        (c for c in session.pending_changes if c.id == msg["change_id"]), None
    )
    if change is None:
        connection.send_error(msg["id"], "not_found", "Change not found")
        return
    result = await tools.apply_pending_change(change)
    if "error" in result:
        connection.send_error(msg["id"], "apply_failed", result["error"])
        return
    await store.remove_pending(msg["session_id"], msg["change_id"])
    connection.send_result(msg["id"], {"ok": True, "result": result})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "claude_chat/reject_change",
        vol.Required("session_id"): str,
        vol.Required("change_id"): str,
    }
)
@websocket_api.async_response
async def ws_reject_change(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    await _store(hass).remove_pending(msg["session_id"], msg["change_id"])
    connection.send_result(msg["id"], {"ok": True})
