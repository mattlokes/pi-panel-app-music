"""The window: the widgets arranged in one of two layouts, and kept in step
with a NowPlaying. No networking and no policy here."""

from __future__ import annotations

import logging
import re
from importlib.resources import as_file, files
from typing import Callable

from gi.repository import Gdk, Gtk, Pango

from ..backends.base import NEXT, PLAY_PAUSE, PREVIOUS, SEEK
from ..config import Display
from ..model import NowPlaying, time_labels
from . import icons
from .widgets import Artwork, Backdrop, Icon, Scrubber

log = logging.getLogger("pi-panel-music")

DESIGN_WIDTH, DESIGN_HEIGHT = 1920, 1080

OnCommand = Callable[[str, float | None], None]


def load_font() -> None:
    """Make the bundled Inter available to this process (Pango 1.56+).
    Without it the CSS falls back to the system's sans."""
    try:
        import gi
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo
        with as_file(files(__package__).joinpath("fonts/InterVariable.ttf")) as font:
            PangoCairo.FontMap.get_default().add_font_file(str(font))
    except Exception as exc:  # noqa: BLE001 - cosmetic: any failure means the fallback font
        log.info("bundled font not loaded (%s); using the system font", exc)


def screen_size() -> tuple[int, int]:
    display = Gdk.Display.get_default()
    monitors = display.get_monitors() if display else None
    if monitors is not None and monitors.get_n_items():
        geometry = monitors.get_item(0).get_geometry()
        if geometry.width > 0 and geometry.height > 0:
            return geometry.width, geometry.height
    return DESIGN_WIDTH, DESIGN_HEIGHT


class PlayerWindow(Gtk.Window):
    def __init__(self, display: Display, size: tuple[int, int], on_command: OnCommand) -> None:
        super().__init__(title="Music")
        self._on_command = on_command
        self._now = NowPlaying()
        self._clock = 0.0
        # One design unit: a pixel on a 1920x1080 screen.
        self.unit = min(size[0] / DESIGN_WIDTH, size[1] / DESIGN_HEIGHT)
        self.set_default_size(*size)
        self.set_decorated(False)
        self.add_css_class("music")
        self._load_css()

        self._paths = {name: icons.path(getattr(icons, name))
                       for name in ("PLAY", "PAUSE", "NEXT", "PREVIOUS", "NOTE")}
        self.backdrop = Backdrop()
        self._status = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
                                 margin_bottom=self.px(34), visible=False)
        self._status.add_css_class("status")
        self._overlay = Gtk.Overlay(child=self.backdrop)
        self._overlay.add_overlay(self._status)
        self.set_child(self._overlay)
        self._content: Gtk.Widget | None = None
        self._art_texture: Gdk.Texture | None = None
        self.set_layout(display)
        self.fullscreen()

    def px(self, design: float) -> int:
        return max(1, round(design * self.unit))

    def _load_css(self) -> None:
        css = files(__package__).joinpath("style.css").read_text(encoding="utf-8")
        css = re.sub(r"(-?\d+(?:\.\d+)?)u\b",
                     lambda m: f"{float(m.group(1)) * self.unit:.2f}px", css)
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # --- building ----------------------------------------------------------

    def _label(self, css_class: str, lines: int = 1, xalign: float = 0.0) -> Gtk.Label:
        label = Gtk.Label(xalign=xalign, ellipsize=Pango.EllipsizeMode.END)
        if lines > 1:
            label.set_wrap(True)
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_lines(lines)
        # Whatever the text, ask for almost no width: the column sets it, and
        # a long title must not push the layout wider.
        label.set_width_chars(1)
        label.set_max_width_chars(1)
        label.set_hexpand(True)
        label.add_css_class(css_class)
        return label

    def _button(self, icon: Icon, size: float, command: str) -> Gtk.Button:
        button = Gtk.Button(child=icon, focusable=False, has_frame=False,
                            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        button.set_size_request(self.px(size), self.px(size))
        button.add_css_class("transport")
        button.connect("clicked", lambda _b: self._on_command(command, None))
        return button

    def set_layout(self, display: Display) -> None:
        """(Re)build the content for |display|, keeping what is shown."""
        if self._content is not None:
            self._overlay.remove_overlay(self._content)
        stacked = self._stacked = display.layout == "stacked"
        px = self.px

        art_size = 560 if stacked else 760
        column_width = 560 if stacked else 780
        self.artwork = Artwork(px(art_size), self._paths["NOTE"])
        self.artwork.set_halign(Gtk.Align.CENTER)
        self.artwork.set_valign(Gtk.Align.CENTER)
        self.artwork.set_texture(self._art_texture)

        self._title = self._label("title", lines=1 if stacked else 2)
        self._artist = self._label("artist")
        self._album = self._label("album")
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=px(4 if stacked else 10))
        text.append(self._title)
        text.append(self._artist)
        if not stacked:                 # the phone layout has no room for it
            text.append(self._album)

        self.scrubber = Scrubber(self.unit, self._seek, self._scrub)
        self.scrubber.add_css_class("scrubber")
        self._elapsed = self._label("time")
        self._remaining = self._label("time", xalign=1.0)
        times = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        times.append(self._elapsed)
        times.append(self._remaining)
        progress = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        progress.append(self.scrubber)
        progress.append(times)

        self._play_icon = Icon(self._paths["PLAY"], px(84))
        self._previous = self._button(Icon(self._paths["PREVIOUS"], px(64)), 124, PREVIOUS)
        self._play = self._button(self._play_icon, 148, PLAY_PAUSE)
        self._next = self._button(Icon(self._paths["NEXT"], px(64)), 124, NEXT)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=px(34 if stacked else 56), halign=Gtk.Align.CENTER)
        for button in (self._previous, self._play, self._next):
            controls.append(button)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        column.set_size_request(px(column_width), -1)
        if stacked:
            content = column
            column.set_halign(Gtk.Align.CENTER)
            column.add_css_class("stacked")
            column.append(self.artwork)
            text.set_margin_top(px(34))
            progress.set_margin_top(px(14))
            controls.set_margin_top(px(2))
        else:
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=px(120),
                              halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            progress.set_margin_top(px(52))
            controls.set_margin_top(px(26))
        column.append(text)
        column.append(progress)
        column.append(controls)
        if not stacked:
            for child in ((self.artwork, column) if display.artwork_side == "left"
                          else (column, self.artwork)):
                content.append(child)

        self._content = content
        self._overlay.add_overlay(content)
        self.update(self._now, self._clock, animate=False)

    # --- showing -----------------------------------------------------------

    def update(self, now_playing: NowPlaying, clock: float, animate: bool = True) -> None:
        self._now, self._clock = now_playing, clock
        has_track = now_playing.has_track
        self._title.set_label(now_playing.title if has_track else "Not Playing")
        self._artist.set_label(now_playing.artist if has_track else "")
        self._album.set_label(now_playing.album if has_track else "")
        # An empty label still takes its line. Beside the artwork, let the
        # others close up; beneath it, keep the line so the artwork stays put.
        self._artist.set_visible(self._stacked or bool(self._artist.get_label()))
        self._album.set_visible(bool(self._album.get_label()))
        if has_track:
            self._content.remove_css_class("idle")
        else:
            self._content.add_css_class("idle")

        self.artwork.set_playing(now_playing.playing, animate=animate)
        self._play_icon.set_path(self._paths["PAUSE" if now_playing.playing else "PLAY"])
        self._play.set_sensitive(has_track)
        self._previous.set_sensitive(has_track and now_playing.can_previous)
        self._next.set_sensitive(has_track and now_playing.can_next)
        self.scrubber.set_seekable(has_track and now_playing.can_seek)
        self.tick(clock)

    def tick(self, clock: float) -> None:
        """Move the progress on to monotonic time |clock|."""
        self._clock = clock
        if self.scrubber.dragging:
            return
        self.scrubber.set_fraction(self._now.fraction(clock) if self._now.has_track else 0.0)
        self._set_times(*time_labels(self._now, clock))

    def _set_times(self, elapsed: str, remaining: str) -> None:
        for label, text in ((self._elapsed, elapsed or "--:--"),
                            (self._remaining, remaining or "--:--")):
            if label.get_label() != text:
                label.set_label(text)

    def set_artwork(self, texture: Gdk.Texture | None, backdrop: Gdk.Texture | None) -> None:
        self._art_texture = texture
        self.artwork.set_texture(texture)
        self.backdrop.set_texture(backdrop)

    def set_status(self, text: str) -> None:
        self._status.set_label(text)
        self._status.set_visible(bool(text))

    # --- the scrubber ------------------------------------------------------

    def _scrub(self, fraction: float | None) -> None:
        """The finger is at |fraction|; None when it lets go."""
        if fraction is None:
            self.tick(self._clock)
        elif self._now.duration:
            self._set_times(*time_labels(self._now, self._clock,
                                         position=fraction * self._now.duration))

    def _seek(self, fraction: float) -> None:
        if self._now.duration:
            self._on_command(SEEK, fraction * self._now.duration)
