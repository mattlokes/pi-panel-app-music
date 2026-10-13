"""Cover art: fetched and decoded off the main loop, and blurred once.

The blur is the expensive part of the Apple Music look. It is rendered a
single time per cover into a small texture, which the backdrop then just
stretches: a still screen costs nothing.
"""

from __future__ import annotations

import logging
import threading
import urllib.request
from collections import OrderedDict
from typing import Callable

from . import ui  # noqa: F401 - pins the GTK versions before the import below
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

log = logging.getLogger("pi-panel-music")

MAX_BYTES = 24 * 1024 * 1024
TIMEOUT = 8.0
# The blurred backdrop is this many pixels square, whatever the screen.
BACKDROP_SIZE = 160
BACKDROP_BLUR = 40.0
BACKDROP_SATURATION = 1.5

# (cover, blurred backdrop); either may be None for a cover that failed.
Loaded = tuple[Gdk.Texture | None, Gdk.Texture | None]


def fetch(url: str) -> bytes:
    if url.startswith("demo:"):
        from .backends.demo import artwork_png
        return artwork_png(int(url.removeprefix("demo:")))
    request = urllib.request.Request(url, headers={"User-Agent": "pi-panel-app-music"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("image too large")
    return data


def saturation(amount: float) -> Graphene.Matrix:
    """The colour matrix of CSS filter: saturate()."""
    s = amount
    return Graphene.Matrix().init_from_float([
        0.213 + 0.787 * s, 0.213 - 0.213 * s, 0.213 - 0.213 * s, 0.0,
        0.715 - 0.715 * s, 0.715 + 0.285 * s, 0.715 - 0.715 * s, 0.0,
        0.072 - 0.072 * s, 0.072 - 0.072 * s, 0.072 + 0.928 * s, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ])


def make_backdrop(texture: Gdk.Texture, renderer: Gsk.Renderer) -> Gdk.Texture:
    """|texture| blurred and saturated, as a small texture to be stretched."""
    size = BACKDROP_SIZE
    # Draw past the edges, so the blur has picture (not transparency) to pull in.
    spill = BACKDROP_BLUR * 2
    bounds = Graphene.Rect().init(0, 0, size, size)
    snapshot = Gtk.Snapshot()
    snapshot.push_clip(bounds)
    snapshot.push_color_matrix(saturation(BACKDROP_SATURATION), Graphene.Vec4.zero())
    snapshot.push_blur(BACKDROP_BLUR)
    snapshot.append_scaled_texture(
        texture, Gsk.ScalingFilter.TRILINEAR,
        Graphene.Rect().init(-spill, -spill, size + 2 * spill, size + 2 * spill))
    snapshot.pop()
    snapshot.pop()
    snapshot.pop()
    return renderer.render_texture(snapshot.to_node(), bounds)


class ArtworkLoader:
    """Loads covers by URL. `on_loaded(url, cover, backdrop)` is called on the
    GLib loop; the caller checks the URL is still the one it wants."""

    CACHE = 6     # recent covers kept: skipping back and forth is instant

    def __init__(self, window: Gtk.Window,
                 on_loaded: Callable[[str, Gdk.Texture | None, Gdk.Texture | None], None]) -> None:
        self._window = window
        self._on_loaded = on_loaded
        self._cache: OrderedDict[str, Loaded] = OrderedDict()
        self._loading: set[str] = set()

    def load(self, url: str) -> None:
        if url in self._cache:
            self._cache.move_to_end(url)
            self._on_loaded(url, *self._cache[url])
        elif url not in self._loading:
            self._loading.add(url)
            # Daemon threads: a slow server must not hold up SIGTERM.
            threading.Thread(target=self._work, args=(url,), name="artwork", daemon=True).start()

    def _work(self, url: str) -> None:
        texture = None
        try:
            # Decoding here too: it is the slow half on a Pi.
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(fetch(url)))
        except Exception as exc:  # noqa: BLE001 - network, HTTP, decoding: all mean "no cover"
            # Not the URL: Home Assistant's carries an access token.
            log.warning("could not load the artwork: %s", exc)
        GLib.idle_add(self._done, url, texture)

    def _done(self, url: str, texture: Gdk.Texture | None) -> bool:
        self._loading.discard(url)
        backdrop = None
        renderer = self._window.get_renderer()
        if texture is not None and renderer is not None:
            try:
                backdrop = make_backdrop(texture, renderer)
            except GLib.Error as exc:
                log.warning("could not blur the artwork: %s", exc)
        if texture is not None:         # a failure is retried on the next track change
            self._cache[url] = (texture, backdrop)
            while len(self._cache) > self.CACHE:
                self._cache.popitem(last=False)
        self._on_loaded(url, texture, backdrop)
        return False
