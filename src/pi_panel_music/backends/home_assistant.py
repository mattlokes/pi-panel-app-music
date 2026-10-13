"""Home Assistant: one media_player entity over the WebSocket API.

`subscribe_entities` sends the entity's state at once and then every change
as a small diff, so nothing polls. Commands are service calls on the same
socket.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from websockets.asyncio.client import ClientConnection

from ..config import HomeAssistant
from ..model import IDLE, PAUSED, PLAYING, NowPlaying
from .base import NEXT, PLAY_PAUSE, PREVIOUS, SEEK, Backend, BackendError, OnStatus, OnUpdate
from .base import websocket_url

log = logging.getLogger("pi-panel-music")

# MediaPlayerEntityFeature bits in the supported_features attribute.
FEATURE_SEEK, FEATURE_PREVIOUS, FEATURE_NEXT = 2, 16, 32

SERVICES = {
    PLAY_PAUSE: "media_play_pause",
    NEXT: "media_next_track",
    PREVIOUS: "media_previous_track",
    SEEK: "media_seek",
}

SUBSCRIPTION = 1      # the message id of our subscribe_entities


def apply_event(entity_id: str, current: dict[str, Any] | None,
                event: dict[str, Any]) -> dict[str, Any] | None:
    """Apply one subscribe_entities event to |current| ({"s": state, "a":
    attributes}, or None while the entity doesn't exist) and return the result.

    The event's keys: "a" adds entities with their full state, "c" changes
    them ("+" sets, "-" lists removed attributes), "r" removes them.
    """
    if entity_id in (event.get("r") or ()):
        current = None
    added = (event.get("a") or {}).get(entity_id)
    if added is not None:
        current = {"s": added.get("s"), "a": dict(added.get("a") or {})}
    change = (event.get("c") or {}).get(entity_id)
    if change and current is not None:
        current = {"s": current["s"], "a": dict(current["a"])}
        plus = change.get("+") or {}
        if "s" in plus:
            current["s"] = plus["s"]
        current["a"].update(plus.get("a") or {})
        for key in (change.get("-") or {}).get("a") or ():
            current["a"].pop(key, None)
    return current


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_entity(entity: dict[str, Any] | None, base_url: str,
                 wall: float, monotonic: float) -> NowPlaying:
    """A media_player's state and attributes as a NowPlaying. |wall| and
    |monotonic| are the same instant on the two clocks: Home Assistant stamps
    the position with wall time."""
    if entity is None:
        return NowPlaying()
    attrs = entity.get("a") or {}
    state = {"playing": PLAYING, "buffering": PLAYING, "paused": PAUSED}.get(entity.get("s"), IDLE)
    if state == IDLE:
        return NowPlaying()

    duration = _number(attrs.get("media_duration"))
    position = _number(attrs.get("media_position"))
    position_at = None
    if position is not None:
        try:
            updated = datetime.fromisoformat(str(attrs.get("media_position_updated_at")))
            position_at = monotonic - max(0.0, wall - updated.timestamp())
        except ValueError:
            position_at = monotonic     # no usable stamp: count from now

    picture = _text(attrs.get("entity_picture"))
    features = attrs.get("supported_features")
    features = features if isinstance(features, int) else 0
    return NowPlaying(
        state=state,
        title=_text(attrs.get("media_title")),
        artist=_text(attrs.get("media_artist")) or _text(attrs.get("media_album_artist")),
        album=_text(attrs.get("media_album_name")),
        # Relative to Home Assistant (its media proxy, signed in the URL), or
        # already absolute.
        artwork_url=urljoin(base_url + "/", picture) if picture else None,
        duration=duration if duration and duration > 0 else None,
        position=position,
        position_at=position_at,
        can_seek=bool(features & FEATURE_SEEK) and bool(duration),
        can_next=bool(features & FEATURE_NEXT),
        can_previous=bool(features & FEATURE_PREVIOUS),
    )


class HomeAssistantBackend(Backend):
    label = "Home Assistant"

    def __init__(self, config: HomeAssistant, on_update: OnUpdate, on_status: OnStatus) -> None:
        super().__init__(on_update, on_status)
        self.config = config
        self.url = websocket_url(config.url, "/api/websocket")
        self._next_id = SUBSCRIPTION + 1

    async def session(self, ws: ClientConnection) -> None:
        hello = await self.receive(ws)
        if hello.get("type") == "auth_required":
            await self.send(ws, {"type": "auth", "access_token": self.config.token})
            hello = await self.receive(ws)
        if hello.get("type") != "auth_ok":
            raise BackendError("Home Assistant refused the access token")

        entity_id = self.config.entity
        self._next_id = SUBSCRIPTION + 1
        await self.send(ws, {"id": SUBSCRIPTION, "type": "subscribe_entities",
                             "entity_ids": [entity_id]})
        entity: dict[str, Any] | None = None
        async for raw in ws:
            message = json.loads(raw)
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "result" and not message.get("success", True):
                error = (message.get("error") or {}).get("message", "unknown error")
                if message.get("id") == SUBSCRIPTION:
                    raise BackendError(f"Home Assistant: {error}")
                log.warning("Home Assistant refused a command: %s", error)
            elif kind == "event" and message.get("id") == SUBSCRIPTION:
                entity = apply_event(entity_id, entity, message.get("event") or {})
                # The first event carries the entity, or nothing if there is none.
                self.ready("" if entity is not None else f"{entity_id} does not exist")
                self.on_update(parse_entity(entity, self.config.url,
                                            time.time(), time.monotonic()))

    async def send_command(self, ws: ClientConnection, name: str, position: float | None) -> None:
        data: dict[str, Any] = {}
        if name == SEEK:
            data["seek_position"] = max(0.0, float(position or 0))
        message_id, self._next_id = self._next_id, self._next_id + 1
        await self.send(ws, {
            "id": message_id, "type": "call_service", "domain": "media_player",
            "service": SERVICES[name], "service_data": data,
            "target": {"entity_id": self.config.entity},
        })
