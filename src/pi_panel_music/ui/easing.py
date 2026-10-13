"""Easing curves, 0..1 -> 0..1 (out_back overshoots on the way)."""

from __future__ import annotations

import math


def out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def in_out_sine(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * t)


def out_back(t: float) -> float:
    """Past the target by about 4%, then back: a cheap spring."""
    c = 1.2
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2
