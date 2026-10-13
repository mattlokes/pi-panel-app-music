"""`--demo`: a pretend player, for working on the screen without a server.

It plays three made-up tracks, answers every command, and paints its own
artwork (`demo:<n>` URLs, which artwork.fetch understands).
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
import zlib
from dataclasses import replace

from ..model import PAUSED, PLAYING, NowPlaying
from .base import NEXT, PLAY_PAUSE, PREVIOUS, SEEK, Backend, OnStatus, OnUpdate

TRACKS = [
    ("Midnight in the Conservatory", "The Paper Lanterns", "Glasshouse", 214.0),
    ("An Unreasonably Long Song Title That Has to Be Cut Short Somewhere",
     "Orchestra of the Eighteenth Parallel & Friends", "Collected Works, Vol. 2", 387.0),
    ("Low Tide", "Marlowe", "Harbour Lights", 181.0),
]

# Two colours and an accent per cover.
PALETTES = [
    ((250, 88, 106), (64, 36, 140), (255, 214, 120)),
    ((24, 160, 150), (12, 40, 84), (240, 250, 200)),
    ((252, 176, 64), (190, 50, 90), (255, 244, 230)),
]


def artwork_png(index: int, size: int = 320) -> bytes:
    """A cover for track |index|: a diagonal gradient, hard-edged bands
    across the lower half (something for the blur to work on) and a sun."""
    a, b, accent = PALETTES[index % len(PALETTES)]
    cx, cy, radius = size * (0.3 + 0.2 * (index % 3)), size * 0.38, size * 0.22
    band = size / (7 + 2 * (index % 3))
    rows = bytearray()
    for y in range(size):
        rows.append(0)                                  # filter: none
        for x in range(size):
            t = (x + y) / (2 * size)
            if y > size * 0.62 and int((x + y * 0.5) / band) % 2:
                t = 1.0 - t
            sun = 1.0 if math.hypot(x - cx, y - cy) < radius else 0.0
            for k in range(3):
                base = a[k] + (b[k] - a[k]) * t
                rows.append(int(base + (accent[k] - base) * sun))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))


class DemoBackend(Backend):
    label = "the demo"

    def __init__(self, on_update: OnUpdate, on_status: OnStatus, start: int = 0,
                 state: str = PLAYING) -> None:
        super().__init__(on_update, on_status)
        self._index = start % len(TRACKS)
        self._state = state
        self._now = NowPlaying()

    def _track(self, position: float = 0.0) -> None:
        title, artist, album, duration = TRACKS[self._index]
        self._now = NowPlaying(
            state=self._state, title=title, artist=artist, album=album,
            artwork_url=f"demo:{self._index}", duration=duration, position=position,
            position_at=time.monotonic(), can_seek=True, can_next=True, can_previous=True)
        self.on_update(self._now)

    async def run(self) -> None:
        if self._state not in (PLAYING, PAUSED):
            self.ready()
            self.on_update(NowPlaying())
            await asyncio.Event().wait()
        self.ready()
        self._track(position=TRACKS[self._index][3] * 0.31)
        while True:
            await asyncio.sleep(1)
            position = self._now.position_now(time.monotonic())
            if self._now.playing and position is not None and position >= (self._now.duration or 0):
                self._index = (self._index + 1) % len(TRACKS)
                self._track()

    async def command(self, name: str, position: float | None = None) -> None:
        now = time.monotonic()
        if name == PLAY_PAUSE:
            self._state = PAUSED if self._state == PLAYING else PLAYING
            self._now = replace(self._now, state=self._state,
                                position=self._now.position_now(now), position_at=now)
            self.on_update(self._now)
        elif name in (NEXT, PREVIOUS):
            self._index = (self._index + (1 if name == NEXT else -1)) % len(TRACKS)
            self._track()
        elif name == SEEK:
            self._now = replace(self._now, position=float(position or 0), position_at=now)
            self.on_update(self._now)
