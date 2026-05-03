"""Scripted fake of the Anthropic streaming SDK for tests.

Each call to `messages.stream(...)` pops the next scripted response off the
queue. Each response is a list of event objects that mimic what the real SDK
yields. Use the helper builders below to construct them.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as N
from typing import Any


def text_event(text: str, *, stop_reason: str = "end_turn") -> list[Any]:
    """One assistant turn that emits plain text and stops."""
    return [
        N(
            type="content_block_start",
            index=0,
            content_block=N(type="text", id=None, name=None),
        ),
        N(
            type="content_block_delta",
            index=0,
            delta=N(type="text_delta", text=text),
        ),
        N(type="content_block_stop", index=0),
        N(type="message_delta", delta=N(stop_reason=stop_reason)),
        N(type="message_stop"),
    ]


def tool_use_event(
    *,
    tool_id: str,
    name: str,
    input_obj: dict,
    preceding_text: str = "",
) -> list[Any]:
    """An assistant turn that calls one tool and stops with stop_reason=tool_use."""
    events: list[Any] = []
    if preceding_text:
        events += [
            N(
                type="content_block_start",
                index=0,
                content_block=N(type="text", id=None, name=None),
            ),
            N(
                type="content_block_delta",
                index=0,
                delta=N(type="text_delta", text=preceding_text),
            ),
            N(type="content_block_stop", index=0),
        ]
    json_str = json.dumps(input_obj)
    events += [
        N(
            type="content_block_start",
            index=1,
            content_block=N(type="tool_use", id=tool_id, name=name),
        ),
        N(
            type="content_block_delta",
            index=1,
            delta=N(type="input_json_delta", partial_json=json_str),
        ),
        N(type="content_block_stop", index=1),
        N(type="message_delta", delta=N(stop_reason="tool_use")),
        N(type="message_stop"),
    ]
    return events


class _FakeStreamCtx:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def __aiter__(self):
        for ev in self._events:
            yield ev


class _FakeMessages:
    def __init__(self, parent: "FakeAsyncAnthropic") -> None:
        self._parent = parent

    def stream(self, **kwargs):
        self._parent.calls.append(kwargs)
        if not self._parent._scripted:
            raise RuntimeError(
                "FakeAsyncAnthropic ran out of scripted responses — "
                "the test made more API calls than expected."
            )
        return _FakeStreamCtx(self._parent._scripted.pop(0))

    async def create(self, **kwargs):  # for the config_flow auth probe
        return N(id="msg_ok")


class FakeAsyncAnthropic:
    def __init__(self) -> None:
        self._scripted: list[list[Any]] = []
        self.calls: list[dict] = []
        self.messages = _FakeMessages(self)

    def script(self, *responses: list[Any]) -> "FakeAsyncAnthropic":
        self._scripted.extend(responses)
        return self
