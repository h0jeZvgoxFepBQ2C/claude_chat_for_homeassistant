"""Helpers for handling image attachments.

Storage layout:
    <config>/claude_chat_media/<session_id>/<uuid>.<ext>

In the session JSON we store small `image_ref` content blocks:
    {"type": "image_ref", "filename": "abc.jpg", "media_type": "image/jpeg"}

When sending to the Anthropic API we read the bytes off disk and convert
to the SDK's `image` content block format. When rendering in the frontend
we expose them via a static HTTP path so the panel can `<img>` them.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import uuid
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MEDIA_DIR_NAME = "claude_chat_media"
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per image
ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def media_root(hass: HomeAssistant) -> str:
    return hass.config.path(MEDIA_DIR_NAME)


def session_dir(hass: HomeAssistant, session_id: str) -> str:
    return os.path.join(media_root(hass), session_id)


def ensure_media_root(hass: HomeAssistant) -> None:
    os.makedirs(media_root(hass), exist_ok=True)


def _validate(media_type: str, data_b64: str) -> tuple[str, bytes]:
    """Validate type + size, return (ext, raw_bytes)."""
    ext = ALLOWED_TYPES.get(media_type)
    if ext is None:
        raise ValueError(f"Unsupported image type: {media_type}")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception as err:  # noqa: BLE001
        raise ValueError(f"Image base64 is not valid: {err}") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image too large ({len(raw)} bytes; max {MAX_IMAGE_BYTES})"
        )
    if len(raw) < 16:
        raise ValueError("Image data is suspiciously small")
    return ext, raw


def save_image(
    hass: HomeAssistant,
    session_id: str,
    media_type: str,
    data_b64: str,
) -> dict[str, Any]:
    """Save one image to disk. Returns an image_ref content block."""
    ext, raw = _validate(media_type, data_b64)
    dir_ = session_dir(hass, session_id)
    os.makedirs(dir_, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(dir_, filename), "wb") as f:
        f.write(raw)
    return {
        "type": "image_ref",
        "filename": filename,
        "media_type": media_type,
        "bytes": len(raw),
    }


def delete_session_media(hass: HomeAssistant, session_id: str) -> None:
    path = session_dir(hass, session_id)
    if os.path.isdir(path):
        try:
            shutil.rmtree(path)
        except OSError:
            _LOGGER.exception("Could not remove media dir for session %s", session_id)


def image_ref_to_anthropic(
    hass: HomeAssistant, session_id: str, block: dict[str, Any]
) -> dict[str, Any] | None:
    """Read the file off disk + base64-encode for the Anthropic API."""
    filename = block.get("filename")
    media_type = block.get("media_type", "image/jpeg")
    if not filename:
        return None
    path = os.path.join(session_dir(hass, session_id), filename)
    if not os.path.exists(path):
        _LOGGER.warning("image_ref missing on disk: %s", path)
        return None
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def to_api_content(
    hass: HomeAssistant, session_id: str, content: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace any image_ref blocks with Anthropic-shaped image blocks."""
    out = []
    for block in content:
        if block.get("type") == "image_ref":
            api_block = image_ref_to_anthropic(hass, session_id, block)
            if api_block is not None:
                out.append(api_block)
        else:
            out.append(block)
    return out
