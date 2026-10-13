"""When to ask pi-panel-ctl for the screen, and when to give it back.

Two halves. `AutoShow` is the policy: no I/O and no clock of its own, so it is
tested with a fake one. `PanelHold` does the asking, over io.pipanel.Ctl, on
the network thread.
"""

from __future__ import annotations

import logging
from typing import Callable

from pi_panel_varlink import Client, ProtocolError, VarlinkError, VarlinkTimeout, VarlinkUnavailable

log = logging.getLogger("pi-panel-music")


class AutoShow:
    """Show when playback starts; release |release_after| seconds after it
    pauses or stops, so a short pause doesn't give the screen away.

    Every start of playback asks again, even if we believe we are still
    shown: someone may have dismissed the app (pi-panel-ctl next drops holds)
    and pressing play should bring it back.
    """

    def __init__(self, release_after: float, show: Callable[[], None],
                 release: Callable[[], None]) -> None:
        self.release_after = release_after
        self._show = show
        self._release = release
        self.playing = False
        self.shown = False
        # When to release (on the caller's clock), while paused and shown.
        self.deadline: float | None = None

    def update(self, playing: bool, now: float) -> None:
        if playing and not self.playing:
            self.shown = True
            self.deadline = None
            self._show()
        elif not playing and self.playing and self.shown:
            self.deadline = now + self.release_after
        self.playing = playing
        self.tick(now)

    def tick(self, now: float) -> None:
        if self.deadline is not None and now >= self.deadline:
            self.release()

    def release(self) -> None:
        """Give the screen back now (also: shutdown, auto switched off)."""
        self.deadline = None
        if self.shown:
            self.shown = False
            self._release()


class PanelHold:
    """At most one io.pipanel.Ctl hold for this app. Coroutines; run them on
    one event loop, where they cannot interleave between awaits that matter:
    each takes the token before it awaits."""

    def __init__(self, address: str, app: str, priority: int) -> None:
        self.client = Client(address, timeout=3.0)
        self.app = app
        self.priority = priority
        self.token: str | None = None

    async def show(self) -> None:
        await self.release()        # never stack holds
        try:
            reply = await self.client.call("io.pipanel.Ctl.Show",
                                           {"app": self.app, "priority": self.priority})
        except (VarlinkError, VarlinkUnavailable, VarlinkTimeout, ProtocolError) as exc:
            log.warning("pi-panel-ctl would not show %s: %s", self.app, exc)
            return
        stale, self.token = self.token, reply.get("token")
        if stale:                   # a second show() got in while we waited
            await self._drop(stale)

    async def release(self) -> None:
        token, self.token = self.token, None
        if token:
            await self._drop(token)

    async def _drop(self, token: str) -> None:
        try:
            await self.client.call("io.pipanel.Ctl.Release", {"token": token})
        except VarlinkError:
            pass                    # NoSuchHold: already dropped (next/previous, a ctl restart)
        except (VarlinkUnavailable, VarlinkTimeout, ProtocolError) as exc:
            # ctl is down, and its holds went with it.
            log.info("could not release the hold: %s", exc)
