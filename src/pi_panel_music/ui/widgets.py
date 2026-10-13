"""The drawn parts of the screen: backdrop, artwork, transport icons, scrubber.

Each is a Gtk.Widget that paints itself in do_snapshot, so GTK's renderer
(the GPU) does the compositing. Animations run on the frame clock only while
something is moving; a still screen draws nothing.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gdk, Graphene, Gsk, Gtk

from . import easing

WHITE = (1.0, 1.0, 1.0)


def rgba(colour: tuple[float, float, float], alpha: float = 1.0) -> Gdk.RGBA:
    out = Gdk.RGBA()
    out.red, out.green, out.blue = colour
    out.alpha = alpha
    return out


def rect(x: float, y: float, w: float, h: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, w, h)


def rounded(bounds: Graphene.Rect, radius: float) -> Gsk.RoundedRect:
    out = Gsk.RoundedRect()
    out.init_from_rect(bounds, radius)
    return out


def cover(texture: Gdk.Texture, x: float, y: float, w: float, h: float) -> Graphene.Rect:
    """Where to draw |texture| so it fills the box, cropping the overflow."""
    tw, th = texture.get_width(), texture.get_height()
    scale = max(w / tw, h / th)
    return rect(x + (w - tw * scale) / 2, y + (h - th * scale) / 2, tw * scale, th * scale)


class Animation:
    """A number that eases to wherever it is sent, redrawing its widget."""

    def __init__(self, widget: Gtk.Widget, value: float, seconds: float,
                 ease: Callable[[float], float] = easing.out_cubic) -> None:
        self._widget = widget
        self._seconds = seconds
        self._ease = ease
        self._from = self._to = self.value = value
        self._start: int | None = None
        self._ticking = False

    @property
    def target(self) -> float:
        return self._to

    @property
    def running(self) -> bool:
        return self._ticking

    def go(self, target: float, ease: Callable[[float], float] | None = None,
           restart: bool = False) -> None:
        if target == self._to and not restart:
            return
        if ease is not None:
            self._ease = ease
        self._from, self._to, self._start = self.value, target, None
        # Unmapped, there is no frame clock (and nobody to see it).
        if not self._widget.get_mapped():
            self.value = target
            self._widget.queue_draw()
        elif not self._ticking:
            self._ticking = True
            self._widget.add_tick_callback(self._tick)

    def jump(self, value: float) -> None:
        self._from = self._to = self.value = value
        self._widget.queue_draw()

    def _tick(self, widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time()
        if self._start is None:
            self._start = now
        t = min(1.0, (now - self._start) / (self._seconds * 1e6))
        self.value = self._from + (self._to - self._from) * self._ease(t)
        widget.queue_draw()
        self._ticking = t < 1.0
        return self._ticking


class Backdrop(Gtk.Widget):
    """The whole-window background: the artwork, blurred beyond recognition
    and dimmed so white text reads on any cover. Cross-fades between tracks."""

    __gtype_name__ = "PiPanelMusicBackdrop"

    FADE_SECONDS = 0.9
    BASE_TOP, BASE_BOTTOM = (0.17, 0.17, 0.19), (0.05, 0.05, 0.06)

    def __init__(self) -> None:
        super().__init__()
        self._old: Gdk.Texture | None = None
        self._new: Gdk.Texture | None = None
        self._fade = Animation(self, 1.0, self.FADE_SECONDS, easing.in_out_sine)

    def set_texture(self, texture: Gdk.Texture | None) -> None:
        if texture is self._new:
            return
        # Mid-fade, start from whichever picture is the more visible.
        self._old = self._new if self._fade.value >= 0.5 else self._old
        self._new = texture
        self._fade.jump(0.0)
        self._fade.go(1.0)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w, h = self.get_width(), self.get_height()
        bounds = rect(0, 0, w, h)
        top, bottom = Gsk.ColorStop(), Gsk.ColorStop()
        top.offset, top.color = 0.0, rgba(self.BASE_TOP)
        bottom.offset, bottom.color = 1.0, rgba(self.BASE_BOTTOM)
        snapshot.append_linear_gradient(bounds, Graphene.Point().init(0, 0),
                                        Graphene.Point().init(0, h), [top, bottom])
        fade = self._fade.value
        snapshot.push_clip(bounds)
        for texture, alpha in ((self._old, 1.0 - fade), (self._new, fade)):
            if texture is None or alpha <= 0.0:
                continue
            snapshot.push_opacity(alpha)
            snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.LINEAR,
                                           cover(texture, 0, 0, w, h))
            snapshot.pop()
        snapshot.pop()
        # Dim it evenly, then a little more towards the bottom.
        snapshot.append_color(rgba((0, 0, 0), 0.38), bounds)
        clear, dark = Gsk.ColorStop(), Gsk.ColorStop()
        clear.offset, clear.color = 0.0, rgba((0, 0, 0), 0.0)
        dark.offset, dark.color = 1.0, rgba((0, 0, 0), 0.28)
        snapshot.append_linear_gradient(bounds, Graphene.Point().init(0, 0),
                                        Graphene.Point().init(0, h), [clear, dark])


class Artwork(Gtk.Widget):
    """The cover: a rounded square with a deep shadow. It shrinks while
    paused and springs back on play, as Apple Music's does."""

    __gtype_name__ = "PiPanelMusicArtwork"

    PAUSED_SCALE = 0.80
    FADE_SECONDS = 0.5
    SCALE_SECONDS = 0.55

    def __init__(self, size: int, note: "Gsk.Path") -> None:
        super().__init__()
        self.set_size_request(size, size)
        self._note = note
        self._old: Gdk.Texture | None = None
        self._new: Gdk.Texture | None = None
        self._fade = Animation(self, 1.0, self.FADE_SECONDS, easing.in_out_sine)
        self._scale = Animation(self, self.PAUSED_SCALE, self.SCALE_SECONDS)

    def set_texture(self, texture: Gdk.Texture | None) -> None:
        if texture is self._new:
            return
        self._old = self._new if self._fade.value >= 0.5 else self._old
        self._new = texture
        self._fade.jump(0.0)
        self._fade.go(1.0)

    def set_playing(self, playing: bool, animate: bool = True) -> None:
        target = 1.0 if playing else self.PAUSED_SCALE
        if not animate:
            self._scale.jump(target)
        else:
            # Growing overshoots a touch and settles; shrinking just eases.
            self._scale.go(target, easing.out_back if playing else easing.out_cubic)

    def _placeholder(self, snapshot: Gtk.Snapshot, box: Graphene.Rect, side: float) -> None:
        x, y = box.get_x(), box.get_y()
        top, bottom = Gsk.ColorStop(), Gsk.ColorStop()
        top.offset, top.color = 0.0, rgba((0.36, 0.36, 0.39))
        bottom.offset, bottom.color = 1.0, rgba((0.20, 0.20, 0.22))
        snapshot.append_linear_gradient(box, Graphene.Point().init(x, y),
                                        Graphene.Point().init(x, y + side), [top, bottom])
        glyph = side * 0.42
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x + (side - glyph) / 2, y + (side - glyph) / 2))
        snapshot.scale(glyph / 24, glyph / 24)
        snapshot.append_fill(self._note, Gsk.FillRule.WINDING, rgba(WHITE, 0.30))
        snapshot.restore()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w, h = self.get_width(), self.get_height()
        full = min(w, h)
        side = full * self._scale.value
        box = rect((w - side) / 2, (h - side) / 2, side, side)
        shape = rounded(box, side * 0.035)
        # The shadow sinks and softens with the cover's size.
        snapshot.append_outset_shadow(shape, rgba((0, 0, 0), 0.42),
                                      0, side * 0.035, 0, side * 0.09)
        snapshot.push_rounded_clip(shape)
        fade = self._fade.value
        for texture, alpha in ((self._old, 1.0 - fade), (self._new, fade)):
            if alpha <= 0.0:
                continue
            snapshot.push_opacity(alpha)
            if texture is None:
                self._placeholder(snapshot, box, side)
            else:
                snapshot.append_scaled_texture(
                    texture, Gsk.ScalingFilter.TRILINEAR,
                    cover(texture, box.get_x(), box.get_y(), side, side))
            snapshot.pop()
        snapshot.pop()
        # A hairline highlight: it lifts a dark cover off a dark backdrop.
        edge = rgba(WHITE, 0.10)
        snapshot.append_border(shape, [1, 1, 1, 1], [edge, edge, edge, edge])


class Icon(Gtk.Widget):
    """A transport glyph, drawn from a path on a 24-unit square."""

    __gtype_name__ = "PiPanelMusicIcon"

    def __init__(self, path: "Gsk.Path", size: int) -> None:
        super().__init__()
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self._path = path
        # A round-joined outline in the fill colour softens every corner.
        self._stroke = Gsk.Stroke.new(1.6)
        self._stroke.set_line_join(Gsk.LineJoin.ROUND)

    def set_path(self, path: "Gsk.Path") -> None:
        if path is not self._path:
            self._path = path
            self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        side = min(self.get_width(), self.get_height())
        snapshot.save()
        snapshot.translate(Graphene.Point().init((self.get_width() - side) / 2,
                                                 (self.get_height() - side) / 2))
        snapshot.scale(side / 24, side / 24)
        white = rgba(WHITE)
        snapshot.append_fill(self._path, Gsk.FillRule.WINDING, white)
        snapshot.append_stroke(self._path, self._stroke, white)
        snapshot.restore()


class Scrubber(Gtk.Widget):
    """The progress bar. A touch anywhere on it (it is taller than the line
    it draws) grabs it: the line thickens and follows the finger, and letting
    go seeks."""

    __gtype_name__ = "PiPanelMusicScrubber"

    def __init__(self, unit: float, on_seek: Callable[[float], None],
                 on_scrub: Callable[[float | None], None]) -> None:
        super().__init__()
        self._thin, self._thick = 9 * unit, 18 * unit
        self.set_size_request(-1, int(56 * unit))
        self.set_hexpand(True)
        self._on_seek = on_seek
        self._on_scrub = on_scrub
        self._fraction = 0.0
        self._seekable = False
        self._drag: float | None = None       # the fraction under the finger
        self._start_x = 0.0
        self._grab = Animation(self, 0.0, 0.18)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        drag.connect("cancel", lambda *_: self._finish(seek=False))
        self.add_controller(drag)

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    def set_fraction(self, fraction: float) -> None:
        fraction = min(1.0, max(0.0, fraction))
        # Redraw only when the line would move by a visible amount.
        if abs(fraction - self._fraction) * max(1, self.get_width()) >= 0.5:
            self._fraction = fraction
            if not self.dragging:
                self.queue_draw()

    def set_seekable(self, seekable: bool) -> None:
        self._seekable = seekable
        if not seekable and self.dragging:
            self._finish(seek=False)

    def _at(self, x: float) -> float:
        return min(1.0, max(0.0, x / max(1, self.get_width())))

    def _begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if not self._seekable:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._start_x = x
        self._drag = self._at(x)
        self._grab.go(1.0)
        self._on_scrub(self._drag)

    def _update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self.dragging:
            self._drag = self._at(self._start_x + dx)
            self.queue_draw()
            self._on_scrub(self._drag)

    def _end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        self._finish(seek=True)

    def _finish(self, seek: bool) -> None:
        if not self.dragging:
            return
        fraction, self._drag = self._drag, None
        self._grab.go(0.0)
        if seek and fraction is not None:
            self._fraction = fraction       # stay put until the player answers
            self._on_seek(fraction)
        self._on_scrub(None)
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w, h = self.get_width(), self.get_height()
        grab = self._grab.value
        thickness = self._thin + (self._thick - self._thin) * grab
        line = rect(0, (h - thickness) / 2, w, thickness)
        snapshot.push_rounded_clip(rounded(line, thickness / 2))
        snapshot.append_color(rgba(WHITE, 0.24), line)
        fraction = self._drag if self._drag is not None else self._fraction
        snapshot.append_color(rgba(WHITE, 0.68 + 0.32 * grab),
                              rect(0, line.get_y(), w * fraction, thickness))
        snapshot.pop()
