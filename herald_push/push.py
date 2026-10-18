"""Seals a message for one phone and posts it to the phone's ntfy topic."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Optional

import httpx

from . import crypto
from .store import Device

log = logging.getLogger(__name__)

MAX_BODY = 4096
MAX_TEXT = 1800
MAX_TEXT_BYTES = 2200
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def message(kind: str, **fields) -> dict:
    out = {"id": str(uuid.uuid4()), "ts": int(time.time()), "type": kind}
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def clip(text: str) -> str:
    """At most MAX_TEXT characters and MAX_TEXT_BYTES of UTF-8, so the sealed envelope (base64 grows it by
    a third) stays under ntfy's 4096-byte message limit."""
    text = text.strip()
    if len(text) <= MAX_TEXT and len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
        return text
    cut = text[: MAX_TEXT - 1]
    while len(cut.encode("utf-8")) > MAX_TEXT_BYTES - 3:
        cut = cut[: max(0, len(cut) - max(1, (len(cut.encode("utf-8")) - MAX_TEXT_BYTES) // 2 + 1))]
    return cut.rstrip() + "…"


def clip_bytes(text: str, limit: int) -> str:
    """[text] cut to at most [limit] bytes of UTF-8, at a character's end."""
    text = text.strip()
    if len(text.encode("utf-8")) <= limit:
        return text
    cut = text.encode("utf-8")[: max(0, limit - 3)].decode("utf-8", "ignore")
    return cut.rstrip() + "…"


def envelope_for(device: Device, payload: dict, sig_key) -> str:
    """The sealed message, its text cut further while it's over ntfy's limit: JSON escaping can still grow a
    clipped text past it (control characters, many short lines). The phone can read the rest over the socket."""
    text = payload.get("text")
    while True:
        plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        env = crypto.seal(plaintext, device.push_topic, crypto.G2P, crypto.decode_public(device.enc_pub), sig_key)
        body = env.to_json()
        if len(body.encode("utf-8")) <= MAX_BODY:
            return body
        if not isinstance(text, str) or len(text) <= 1:
            raise ValueError("push message too large")
        text = text[: len(text) * 3 // 4 - 1].rstrip() + "…"
        payload = dict(payload, text=text)


def publish(device: Device, payload: dict, sig_key, client: Optional[httpx.Client] = None) -> bool:
    """True when ntfy took it. No title, tags or priority headers: they'd travel in the clear."""
    try:
        body = envelope_for(device, payload, sig_key)
    except ValueError:
        log.warning("herald-push: a %s message is too large to send", payload.get("type"))
        return False
    url = f"{device.server}/{device.push_topic}"
    try:
        if client is None:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as own:
                response = own.post(url, content=body.encode("utf-8"), headers={"Content-Type": "text/plain"})
        else:
            response = client.post(url, content=body.encode("utf-8"), headers={"Content-Type": "text/plain"})
    except httpx.HTTPError as exc:
        log.warning("herald-push: couldn't reach %s: %s", device.server, type(exc).__name__)
        return False
    if response.status_code >= 300:
        log.warning("herald-push: %s answered %s", device.server, response.status_code)
        return False
    return True
