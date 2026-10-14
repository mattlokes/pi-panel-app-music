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
    """At most one io.pipanel.Ctl hold for this app once each call is done.
    Coroutines; run them on one event loop, where they cannot interleave
    between awaits that matter.

    show() asks for the new hold before it drops the old one. The other way
    round, the app holds nothing for a moment, ctl switches to the next app
    and straight back: a fade out and in at every track change, since players
    report a brief pause between tracks."""

    def __init__(self, address: str, app: str, priority: int) -> None:
        self.client = Client(address, timeout=3.0)
        self.app = app
        self.priority = priority
        self.token: str | None = None
        # The last call was show(), not release(). A release() while a Show
        # is waiting for its reply drops the hold that reply brings.
        self.wanted = False

    async def show(self) -> None:
        self.wanted = True
        previous = self.token
        try:
            reply = await self.client.call("io.pipanel.Ctl.Show",
                                           {"app": self.app, "priority": self.priority})
        except (VarlinkError, VarlinkUnavailable, VarlinkTimeout, ProtocolError) as exc:
            log.warning("pi-panel-ctl would not show %s: %s", self.app, exc)
            return
        token = reply.get("token")
        if not self.wanted:         # released while we waited
            if token:
                await self._drop(token)
            return
        # Swap, then drop the old hold, and one a second show() running at
        # the same time took meanwhile.
        current, self.token = self.token, token
        for stale in dict.fromkeys((previous, current)):
            if stale and stale != token:
                await self._drop(stale)

    async def release(self) -> None:
        self.wanted = False
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
