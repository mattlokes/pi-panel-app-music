"""The part every backend shares: stay connected, report, take commands.

A backend runs as one asyncio task on the network thread. It calls
`on_update` with a NowPlaying whenever the player changes and `on_status`
when the connection comes or goes; both are called on the network thread, and
the app hands them to the GLib loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from ..model import NowPlaying

log = logging.getLogger("pi-panel-music")

OnUpdate = Callable[[NowPlaying], None]
# (connected, what is wrong, or "" when nothing is)
OnStatus = Callable[[bool, str], None]

PLAY_PAUSE, NEXT, PREVIOUS, SEEK = "play_pause", "next", "previous", "seek"

BACKOFF_FIRST, BACKOFF_MAX = 2.0, 30.0


class BackendError(Exception):
    """The server answered, but not usefully: a bad token, a missing player.
    The message is shown on screen."""


def websocket_url(url: str, path: str) -> str:
    """http://host:8123 -> ws://host:8123/api/websocket"""
    return "ws" + url.removeprefix("http").rstrip("/") + path


class Backend:
    label = "the server"      # in "cannot reach …" messages

    def __init__(self, on_update: OnUpdate, on_status: OnStatus) -> None:
        self.on_update = on_update
        self.on_status = on_status
        self.connected = False
        self._ws: ClientConnection | None = None
        self._reported: tuple[bool, str] | None = None

    # --- for subclasses ----------------------------------------------------

    url: str = ""

    async def session(self, ws: ClientConnection) -> None:
        """Log in, subscribe, and handle messages until the socket closes.
        Call self.ready() once updates are flowing."""
        raise NotImplementedError

    async def send_command(self, ws: ClientConnection, name: str, position: float | None) -> None:
        raise NotImplementedError

    def ready(self, problem: str = "") -> None:
        """Connected and following the player; |problem| is for something
        short of that, e.g. the configured player doesn't exist."""
        self.connected = True
        self._report(True, problem)

    def _report(self, connected: bool, problem: str) -> None:
        if (connected, problem) != self._reported:
            self._reported = (connected, problem)
            self.on_status(connected, problem)

    @staticmethod
    async def send(ws: ClientConnection, message: dict[str, Any]) -> None:
        await ws.send(json.dumps(message))

    @staticmethod
    async def receive(ws: ClientConnection) -> dict[str, Any]:
        message = json.loads(await ws.recv())
        if not isinstance(message, dict):
            raise BackendError("unexpected reply from the server")
        return message

    # --- the connection ----------------------------------------------------

    async def run(self) -> None:
        """Connect and stay connected, until cancelled."""
        delay = BACKOFF_FIRST
        while True:
            try:
                # The keepalive ping notices a server that went away silently.
                async with connect(self.url, open_timeout=10, ping_interval=20,
                                   ping_timeout=20, max_size=None) as ws:
                    self._ws = ws
                    await self.session(ws)
                problem = f"lost the connection to {self.label}"
            except BackendError as exc:
                problem = str(exc)
            except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
                problem = (f"lost the connection to {self.label}" if self.connected
                           else f"cannot reach {self.label}")
                log.warning("%s: %s", problem, exc or type(exc).__name__)
            except (ValueError, KeyError, TypeError) as exc:     # a reply we can't read
                problem = f"unexpected reply from {self.label}"
                log.warning("%s: %r", problem, exc)
            finally:
                self._ws = None
            if self.connected:
                delay = BACKOFF_FIRST       # it worked for a while: start over
            self.connected = False
            self._report(False, problem)
            # Nothing is known about the player now; don't show a stale track.
            self.on_update(NowPlaying())
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)

    async def command(self, name: str, position: float | None = None) -> None:
        ws = self._ws
        if ws is None or not self.connected:
            log.info("%s ignored: not connected", name)
            return
        try:
            await self.send_command(ws, name, position)
        except (OSError, WebSocketException) as exc:
            log.warning("%s failed: %s", name, exc)
