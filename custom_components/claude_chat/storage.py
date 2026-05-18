"""Persistent session storage for Claude Chat."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION


@dataclass
class Message:
    role: str
    content: list[dict[str, Any]]
    created_at: float = field(default_factory=time.time)


@dataclass
class PendingChange:
    """A staged change. Stays in the session as history after the user acts.

    status:
      - "pending"  — awaiting Apply / Reject (initial state)
      - "accepted" — user clicked Apply and it succeeded
      - "rejected" — user clicked Reject
    """

    id: str
    kind: str
    summary: str
    payload: dict[str, Any]
    diff: str | None = None
    # The Anthropic tool_use_id that produced this pending change. Used by
    # the frontend to render the card directly after the corresponding
    # tool_use block in the chat — so a new proposal lands at the bottom,
    # not at the top of the chat.
    source_tool_use_id: str | None = None
    status: str = "pending"


@dataclass
class Session:
    id: str
    title: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[Message] = field(default_factory=list)
    pending_changes: list[PendingChange] = field(default_factory=list)


class SessionStore:
    """Wraps HA's Store helper for chat sessions."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._sessions: dict[str, Session] = {}
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        data = await self._store.async_load() or {}
        for raw in data.get("sessions", []):
            session = Session(
                id=raw["id"],
                title=raw["title"],
                created_at=raw.get("created_at", time.time()),
                updated_at=raw.get("updated_at", time.time()),
                messages=[Message(**m) for m in raw.get("messages", [])],
                pending_changes=[
                    PendingChange(**p) for p in raw.get("pending_changes", [])
                ],
            )
            self._sessions[session.id] = session
        self._loaded = True

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "sessions": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "created_at": s.created_at,
                        "updated_at": s.updated_at,
                        "messages": [asdict(m) for m in s.messages],
                        "pending_changes": [asdict(p) for p in s.pending_changes],
                    }
                    for s in self._sessions.values()
                ]
            }
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = sorted(
            self._sessions.values(), key=lambda s: s.updated_at, reverse=True
        )
        return [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "message_count": len(s.messages),
                "has_pending": any(c.status == "pending" for c in s.pending_changes),
            }
            for s in sessions
        ]

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def get_or_raise(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    async def create(self, title: str = "New chat") -> Session:
        session = Session(id=uuid.uuid4().hex, title=title)
        self._sessions[session.id] = session
        await self.async_save()
        return session

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        await self.async_save()

    async def rename(self, session_id: str, title: str) -> None:
        session = self.get_or_raise(session_id)
        session.title = title
        session.updated_at = time.time()
        await self.async_save()

    async def append_message(self, session_id: str, message: Message) -> None:
        session = self.get_or_raise(session_id)
        session.messages.append(message)
        session.updated_at = time.time()
        await self.async_save()

    async def add_pending(
        self, session_id: str, change: PendingChange
    ) -> None:
        session = self.get_or_raise(session_id)
        session.pending_changes.append(change)
        await self.async_save()

    async def remove_pending(self, session_id: str, change_id: str) -> PendingChange | None:
        session = self.get_or_raise(session_id)
        for i, change in enumerate(session.pending_changes):
            if change.id == change_id:
                removed = session.pending_changes.pop(i)
                await self.async_save()
                return removed
        return None

    async def set_change_status(
        self, session_id: str, change_id: str, status: str
    ) -> PendingChange | None:
        """Mark a change as accepted / rejected without removing it."""
        session = self.get_or_raise(session_id)
        for change in session.pending_changes:
            if change.id == change_id:
                change.status = status
                await self.async_save()
                return change
        return None
