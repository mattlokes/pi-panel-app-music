"""Music Assistant, over its WebSocket API (/ws).

The server says hello with its schema version; from schema 28 (server 2.7) a
token must be sent first. After that `player_queues/all` gives every queue,
and `queue_updated` / `queue_time_updated` events keep them current. A queue
has the same id as its player.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote

from websockets.asyncio.client import ClientConnection

from ..config import MusicAssistant
from ..model import IDLE, PAUSED, PLAYING, NowPlaying
from .base import NEXT, PLAY_PAUSE, PREVIOUS, SEEK, Backend, BackendError, OnStatus, OnUpdate
from .base import websocket_url

log = logging.getLogger("pi-panel-music")

SCHEMA_AUTH = 28          # a token is required
SCHEMA_PROXY_ID = 31      # /imageproxy/<proxy_id>
ARTWORK_SIZE = 1024

COMMANDS = {
    PLAY_PAUSE: "player_queues/play_pause",
    NEXT: "player_queues/next",
    PREVIOUS: "player_queues/previous",
    SEEK: "player_queues/seek",
}


def select_queue(queues: dict[str, dict[str, Any]], wanted: str,
                 current: str | None) -> str | None:
    """The id of the queue to show. |wanted| is a player's name or id; empty
    follows whichever is playing, staying on |current| while that still is."""
    if wanted:
        folded = wanted.casefold()
        for queue_id, queue in queues.items():
            if queue_id == wanted or str(queue.get("display_name", "")).casefold() == folded:
                return queue_id
        return None

    def playing(queue_id: str | None) -> bool:
        return queue_id in queues and queues[queue_id].get("state") == PLAYING

    if playing(current):
        return current
    for queue_id in queues:
        if playing(queue_id):
            return queue_id
    if current in queues:
        return current
    # Nothing is playing and nothing was: a paused queue, if there is one.
    for queue_id, queue in queues.items():
        if queue.get("state") == PAUSED and queue.get("current_item"):
            return queue_id
    return None


def image_url(image: Any, base_url: str, schema: int, size: int = ARTWORK_SIZE) -> str | None:
    """Where to fetch a MediaItemImage: itself when it is a public URL, else
    the server's image proxy (as the official client builds it)."""
    if not isinstance(image, dict) or not image.get("path"):
        return None
    path = str(image["path"])
    if image.get("remotely_accessible") and path.startswith(("http://", "https://")):
        return path
    if schema >= SCHEMA_PROXY_ID and image.get("proxy_id"):
        return f"{base_url}/imageproxy/{quote(str(image['proxy_id']), safe='')}?size={size}"
    encoded = quote(quote(path))      # the server unquotes twice
    provider = quote(str(image.get("provider", "")), safe="")
    return f"{base_url}/imageproxy?path={encoded}&provider={provider}&size={size}"


def _item_image(item: dict[str, Any]) -> Any:
    """The queue item's own image, else its track's or album's."""
    if item.get("image"):
        return item["image"]
    media = item.get("media_item") or {}
    for holder in (media, media.get("album") or {}):
        if holder.get("image"):
            return holder["image"]
        images = (holder.get("metadata") or {}).get("images") or []
        for image in images:
            if isinstance(image, dict) and image.get("type") == "thumb":
                return image
        if images:
            return images[0]
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_queue(queue: dict[str, Any] | None, base_url: str, schema: int,
                wall: float, monotonic: float) -> NowPlaying:
    """A PlayerQueue as a NowPlaying. |wall| and |monotonic| are the same
    instant on the two clocks: the server stamps elapsed_time with wall time."""
    if not queue:
        return NowPlaying()
    item = queue.get("current_item")
    state = queue.get("state")
    if state not in (PLAYING, PAUSED) or not isinstance(item, dict):
        return NowPlaying()

    media = item.get("media_item") or {}
    title = str(media.get("name") or "").strip()
    artist = ", ".join(str(a.get("name")) for a in media.get("artists") or []
                       if isinstance(a, dict) and a.get("name"))
    album = str((media.get("album") or {}).get("name") or "").strip()
    if not title:
        # No library item behind it (a radio stream, a URL): the queue item's
        # own name, which is "Artist - Title" when the stream says so.
        name = str(item.get("name") or "").strip()
        head, sep, tail = name.partition(" - ")
        artist, title = (head.strip(), tail.strip()) if sep and tail.strip() else ("", name)

    duration = _number(item.get("duration")) or _number(media.get("duration"))
    position = _number(queue.get("elapsed_time"))
    position_at = None
    if position is not None:
        stamp = _number(queue.get("elapsed_time_last_updated"))
        position_at = monotonic - max(0.0, wall - stamp) if stamp else monotonic

    items = queue.get("items")
    index = queue.get("current_index")
    return NowPlaying(
        state=state,
        title=title,
        artist=artist,
        album=album,
        artwork_url=image_url(_item_image(item), base_url, schema),
        duration=duration if duration and duration > 0 else None,
        position=position,
        position_at=position_at,
        can_seek=bool(duration and duration > 0),
        # Music Assistant restarts the track on "previous" when there is none.
        can_previous=True,
        can_next=not (isinstance(items, int) and isinstance(index, int) and index >= items - 1
                      and not queue.get("radio_source")
                      and queue.get("repeat_mode", "off") == "off"),
    )


class MusicAssistantBackend(Backend):
    label = "Music Assistant"

    def __init__(self, config: MusicAssistant, on_update: OnUpdate, on_status: OnStatus) -> None:
        super().__init__(on_update, on_status)
        self.config = config
        self.url = websocket_url(config.url, "/ws")
        self._schema = 0
        self._queues: dict[str, dict[str, Any]] = {}
        self._current: str | None = None
        self._next_id = 0

    async def _call(self, ws: ClientConnection, command: str, **args: Any) -> Any:
        """One command during the handshake: wait for its result. Events that
        arrive meanwhile are older than the queue list we are about to fetch."""
        self._next_id += 1
        message_id = f"setup-{self._next_id}"
        await self.send(ws, {"message_id": message_id, "command": command, "args": args})
        while True:
            reply = await self.receive(ws)
            if reply.get("message_id") != message_id:
                continue
            if "error_code" in reply:
                raise BackendError(f"Music Assistant: {reply.get('details') or reply['error_code']}")
            if not reply.get("partial"):
                return reply.get("result")

    async def session(self, ws: ClientConnection) -> None:
        info = await self.receive(ws)
        self._schema = int(info.get("schema_version") or 0)
        log.info("Music Assistant %s (schema %s)", info.get("server_version", "?"), self._schema)
        if self._schema >= SCHEMA_AUTH:
            if not self.config.token:
                raise BackendError("Music Assistant needs a token (see config.toml)")
            try:
                await self._call(ws, "auth", token=self.config.token)
            except BackendError:
                raise BackendError("Music Assistant refused the token") from None

        queues = await self._call(ws, "player_queues/all") or []
        self._queues = {str(q["queue_id"]): q for q in queues
                        if isinstance(q, dict) and q.get("queue_id")}
        self._current = None
        self._publish()
        async for raw in ws:
            message = json.loads(raw)
            if not isinstance(message, dict):
                continue
            if "event" in message:
                if self._on_event(message):
                    self._publish()
            elif "error_code" in message:
                log.warning("Music Assistant refused a command: %s",
                            message.get("details") or message["error_code"])

    def _on_event(self, message: dict[str, Any]) -> bool:
        """Take one event in; True if the queues changed."""
        event, queue_id, data = message["event"], message.get("object_id"), message.get("data")
        if event in ("queue_added", "queue_updated") and isinstance(data, dict):
            self._queues[str(data.get("queue_id") or queue_id)] = data
        elif event == "queue_time_updated" and queue_id in self._queues:
            elapsed = data if isinstance(data, (int, float)) else None
            if elapsed is None:
                return False
            self._queues[queue_id] = {**self._queues[queue_id], "elapsed_time": elapsed,
                                      "elapsed_time_last_updated": time.time()}
        elif event == "queue_removed" and queue_id in self._queues:
            del self._queues[queue_id]
        else:
            return False
        return True

    def _publish(self) -> None:
        wanted = self.config.player
        self._current = select_queue(self._queues, wanted, self._current)
        missing = bool(wanted) and self._current is None
        self.ready(f"no player called \"{wanted}\"" if missing else "")
        queue = self._queues.get(self._current) if self._current else None
        self.on_update(parse_queue(queue, self.config.url, self._schema,
                                   time.time(), time.monotonic()))

    async def send_command(self, ws: ClientConnection, name: str, position: float | None) -> None:
        if self._current is None:
            log.info("%s ignored: no player", name)
            return
        args: dict[str, Any] = {"queue_id": self._current}
        if name == SEEK:
            args["position"] = max(0, int(position or 0))
        self._next_id += 1
        await self.send(ws, {"message_id": f"cmd-{self._next_id}",
                             "command": COMMANDS[name], "args": args})
