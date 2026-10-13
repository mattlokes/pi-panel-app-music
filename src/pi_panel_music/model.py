"""What is playing, in the one shape both backends produce."""

from __future__ import annotations

from dataclasses import dataclass

PLAYING, PAUSED, IDLE = "playing", "paused", "idle"


@dataclass(frozen=True, slots=True)
class NowPlaying:
    state: str = IDLE
    title: str = ""
    artist: str = ""
    album: str = ""
    artwork_url: str | None = None
    duration: float | None = None
    # The position as last reported, and when (time.monotonic()). While
    # playing, the position now is worked out from the two: nothing polls.
    position: float | None = None
    position_at: float | None = None
    can_seek: bool = False
    can_next: bool = False
    can_previous: bool = False

    @property
    def playing(self) -> bool:
        return self.state == PLAYING

    @property
    def has_track(self) -> bool:
        return self.state != IDLE and bool(self.title)

    def position_now(self, now: float) -> float | None:
        """The position at monotonic time |now|, kept within the track."""
        if self.position is None:
            return None
        position = self.position
        if self.playing and self.position_at is not None:
            position += max(0.0, now - self.position_at)
        if self.duration:
            position = min(position, self.duration)
        return max(0.0, position)

    def fraction(self, now: float) -> float:
        position = self.position_now(now)
        if position is None or not self.duration:
            return 0.0
        return min(1.0, position / self.duration)


def clock_text(seconds: float) -> str:
    """1:05, 12:40, 1:02:03."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def time_labels(now_playing: NowPlaying, now: float,
                position: float | None = None) -> tuple[str, str]:
    """Elapsed and remaining ("1:05", "−2:40", with a true minus sign), as
    Apple Music shows them.
    |position| overrides the playing position while the scrubber is dragged."""
    if position is None:
        position = now_playing.position_now(now)
    if position is None or not now_playing.has_track:
        return "", ""
    if not now_playing.duration:
        return clock_text(position), ""
    return clock_text(position), "\u2212" + clock_text(now_playing.duration - position)
