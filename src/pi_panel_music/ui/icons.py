"""The glyphs, as SVG path data on a 24-unit square. Drawn for this app (no
icon theme, no SVG loader needed); widgets.Icon rounds their corners."""

from __future__ import annotations

from gi.repository import Gsk

PLAY = "M7.5 4.6 L19.6 12 L7.5 19.4 Z"
PAUSE = "M6.6 5 H10 V19 H6.6 Z M14 5 H17.4 V19 H14 Z"
# Two triangles each, like Apple's forward.fill / backward.fill.
NEXT = "M2.6 6.4 L11.6 12 L2.6 17.6 Z M12.4 6.4 L21.4 12 L12.4 17.6 Z"
PREVIOUS = "M21.4 6.4 L12.4 12 L21.4 17.6 Z M11.6 6.4 L2.6 12 L11.6 17.6 Z"
# Two beamed quavers: the beam and stems, then the two heads. Every subpath
# runs clockwise: with the winding fill rule, overlaps then stay filled.
NOTE = ("M9 5 L19 3 V15 H17.6 V7.2 L10.4 8.6 V18 H9 Z "
        "M4 18 a3.2 2.5 0 1 1 6.4 0 a3.2 2.5 0 1 1 -6.4 0 Z "
        "M12.6 15 a3.2 2.5 0 1 1 6.4 0 a3.2 2.5 0 1 1 -6.4 0 Z")


def path(data: str) -> Gsk.Path:
    parsed = Gsk.Path.parse(data)
    if parsed is None:
        raise ValueError(f"bad path data: {data!r}")
    return parsed
