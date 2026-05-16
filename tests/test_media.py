"""Tests for image attachment helpers."""
from __future__ import annotations

import base64
import os
import struct
import zlib

import pytest

from custom_components.claude_chat.media import (
    delete_session_media,
    image_ref_to_anthropic,
    save_image,
    session_dir,
    to_api_content,
)


def _tiny_png_b64() -> str:
    """Build a minimal valid 1x1 PNG and base64-encode it."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\x00\x00"  # 1 row: filter byte + RGB pixel
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return base64.b64encode(sig + ihdr + idat + iend).decode()


def test_save_and_load_round_trip(hass):
    ref = save_image(hass, "sess1", "image/png", _tiny_png_b64())
    assert ref["type"] == "image_ref"
    assert ref["filename"].endswith(".png")
    assert ref["media_type"] == "image/png"

    block = image_ref_to_anthropic(hass, "sess1", ref)
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert len(block["source"]["data"]) > 10


def test_rejects_disallowed_type(hass):
    with pytest.raises(ValueError, match="Unsupported"):
        save_image(hass, "s", "image/tiff", _tiny_png_b64())


def test_rejects_corrupt_base64(hass):
    with pytest.raises(ValueError):
        save_image(hass, "s", "image/png", "***notbase64***")


def test_to_api_content_swaps_image_refs(hass):
    ref = save_image(hass, "sess2", "image/png", _tiny_png_b64())
    content = [ref, {"type": "text", "text": "hi"}]
    out = to_api_content(hass, "sess2", content)
    assert out[0]["type"] == "image"
    assert out[1]["type"] == "text"


def test_delete_session_media_removes_dir(hass):
    save_image(hass, "sessX", "image/png", _tiny_png_b64())
    path = session_dir(hass, "sessX")
    assert os.path.isdir(path)
    delete_session_media(hass, "sessX")
    assert not os.path.isdir(path)
