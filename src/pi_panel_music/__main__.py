"""pi-panel-app-music: what is playing, Apple Music style.

Three threads. GTK and all state live on the main (GLib) loop. The backend
and pi-panel-ctl calls run on the network thread's asyncio loop. The
io.pipanel.App service answers on its own. The other two hand everything to
the main loop with GLib.idle_add.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from pi_panel_varlink import AppService, Call, Interface, VarlinkError

from . import backends
from .autoshow import AutoShow, PanelHold
from .backends.base import NEXT, PLAY_PAUSE, PREVIOUS, SEEK, Backend
from .config import LAYOUTS, Config, ConfigError
from .config import Display as DisplayConfig
from .model import IDLE, PAUSED, PLAYING, NowPlaying
from .net import NetThread

log = logging.getLogger("pi-panel-music")

VERSION = "0.1.0"
TICK_MS = 250             # how often the progress line moves, while it is seen moving


class Player:
    """The app: one window, one backend, and the policy between them. Every
    method runs on the GLib loop."""

    def __init__(self, args: argparse.Namespace, config: Config, loop: Any) -> None:
        from gi.repository import GLib

        from .artwork import ArtworkLoader
        from .ui.window import PlayerWindow, load_font, screen_size

        self._glib = GLib
        self.args = args
        self.config = config
        self.loop = loop
        self.net = NetThread()
        self.now = NowPlaying()
        self.connected = False
        self.problem = ""
        self.visible = False
        self.backend: Backend | None = None
        self._backend_task: concurrent.futures.Future | None = None
        self._artwork_url: str | None = None
        self._tick_source = 0
        self._release_source = 0
        self.hold: PanelHold | None = None
        self.autoshow: AutoShow | None = None

        load_font()
        self.window = PlayerWindow(self._display(), args.size or screen_size(), self.command)
        self.artwork = ArtworkLoader(self.window, self._on_artwork)

    def _display(self) -> DisplayConfig:
        if self.args.layout:
            return replace(self.config.display, layout=self.args.layout)
        return self.config.display

    @property
    def backend_name(self) -> str:
        return "demo" if self.args.demo else self.config.backend

    # --- starting and stopping ---------------------------------------------

    def start(self) -> None:
        self.net.start()
        self.window.present()
        self._start_autoshow()
        self._start_backend()

    def _start_backend(self) -> None:
        def on_update(now_playing: NowPlaying) -> None:
            self._glib.idle_add(self._on_update, backend, now_playing)

        def on_status(connected: bool, problem: str) -> None:
            self._glib.idle_add(self._on_status, backend, connected, problem)

        if self.args.demo:
            from .backends.demo import DemoBackend
            backend: Backend = DemoBackend(on_update, on_status, start=self.args.demo_track,
                                           state=self.args.demo)
        else:
            backend = backends.create(self.config, on_update, on_status)
        self.backend = backend
        self._backend_task = self.net.submit(backend.run())

    def _stop_backend(self) -> None:
        if self._backend_task is not None:
            self._backend_task.cancel()     # closes its socket on the way out
        self.backend = self._backend_task = None

    def _start_autoshow(self) -> None:
        address = os.environ.get("PI_PANEL_CTL_SOCKET")
        show = self.config.show
        if not show.auto or not address:
            return
        hold = self.hold = PanelHold(address, os.environ.get("PI_PANEL_NAME", "music"),
                                     show.priority)
        self.autoshow = AutoShow(show.release_after,
                                 show=lambda: self.net.submit(hold.show()),
                                 release=lambda: self.net.submit(hold.release()))

    def _stop_autoshow(self) -> None:
        """Give the screen back, and wait until pi-panel-ctl has heard."""
        if self._release_source:
            self._glib.source_remove(self._release_source)
            self._release_source = 0
        if self.hold is not None:
            try:
                self.net.submit(self.hold.release()).result(timeout=3)
            except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
                pass
        self.hold = self.autoshow = None

    def shutdown(self) -> None:
        self._set_ticking(False)
        self._stop_backend()
        self._stop_autoshow()
        self.net.stop()

    def reload(self) -> str | None:
        """Re-read config.toml. Returns what is wrong with it, or None."""
        if self.args.config is None:
            return "--demo has no config to reload"
        try:
            config = Config.load(self.args.config)
        except ConfigError as exc:
            log.error("reload failed, keeping the running config: %s", exc)
            return str(exc)
        self._stop_backend()
        self._stop_autoshow()
        self.config = config
        self.connected, self.problem = False, ""
        self.window.set_layout(self._display())
        self._show(NowPlaying())
        self._start_autoshow()
        self._start_backend()
        log.info("config reloaded: backend %s, layout %s", self.backend_name,
                 self._display().layout)
        return None

    # --- from the backend --------------------------------------------------

    def _on_update(self, backend: Backend, now_playing: NowPlaying) -> bool:
        if backend is self.backend:         # not one a reload has replaced
            self._show(now_playing)
        return False

    def _on_status(self, backend: Backend, connected: bool, problem: str) -> bool:
        if backend is self.backend:
            if (connected, problem) != (self.connected, self.problem):
                log.info("connected" if connected and not problem else problem)
            self.connected, self.problem = connected, problem
            self.window.set_status(problem)
        return False

    def _show(self, now_playing: NowPlaying) -> None:
        clock = time.monotonic()
        previous, self.now = self.now, now_playing
        if (previous.state, previous.title, previous.artist) != (
                now_playing.state, now_playing.title, now_playing.artist):
            log.info("%s: %s", now_playing.state,
                     f"{now_playing.artist} - {now_playing.title}" if now_playing.has_track else "-")
        self.window.update(now_playing, clock)

        url = now_playing.artwork_url if now_playing.has_track else None
        if url != self._artwork_url:
            self._artwork_url = url
            if url is None:
                self.window.set_artwork(None, None)
            else:
                # The old cover stays up until the new one is ready to fade in.
                self.artwork.load(url)

        if self.autoshow is not None:
            self.autoshow.update(now_playing.playing, clock)
            self._arm_release()
        self._set_ticking(self.visible and now_playing.playing)

    def _on_artwork(self, url: str, texture: Any, backdrop: Any) -> None:
        if url == self._artwork_url:
            self.window.set_artwork(texture, backdrop)

    # --- timers ------------------------------------------------------------

    def _arm_release(self) -> None:
        """One timer for the moment the paused app gives the screen back."""
        if self._release_source:
            self._glib.source_remove(self._release_source)
            self._release_source = 0
        deadline = self.autoshow.deadline if self.autoshow else None
        if deadline is not None:
            wait = max(0.0, deadline - time.monotonic())
            self._release_source = self._glib.timeout_add(int(wait * 1000) + 50, self._release_due)

    def _release_due(self) -> bool:
        self._release_source = 0
        if self.autoshow is not None:
            self.autoshow.tick(time.monotonic())
        return False

    def _set_ticking(self, ticking: bool) -> None:
        if ticking and not self._tick_source:
            self._tick_source = self._glib.timeout_add(TICK_MS, self._tick)
        elif not ticking and self._tick_source:
            self._glib.source_remove(self._tick_source)
            self._tick_source = 0

    def _tick(self) -> bool:
        self.window.tick(time.monotonic())
        return True

    # --- from the panel and the screen -------------------------------------

    def set_visible(self, visible: bool) -> None:
        self.visible = visible
        if visible:
            self.window.tick(time.monotonic())
        self._set_ticking(visible and self.now.playing)

    def command(self, name: str, position: float | None = None) -> None:
        if self.backend is None:
            return
        if name == SEEK and position is not None:
            # Hold the line where it was dropped until the player reports.
            self.now = replace(self.now, position=position, position_at=time.monotonic())
            self.window.update(self.now, time.monotonic())
        self.net.submit(self.backend.command(name, position))

    def status(self) -> dict[str, Any]:
        now = self.now
        return {
            "backend": self.backend_name,
            "connected": self.connected,
            "problem": self.problem or None,
            "visible": self.visible,
            "holding": bool(self.hold and self.hold.token),
            "state": now.state if now.state in (PLAYING, PAUSED) else IDLE,
            "title": now.title or None,
            "artist": now.artist or None,
            "album": now.album or None,
            "duration": now.duration,
            "position": now.position_now(time.monotonic()),
        }


def music_interface(player: Player, on_main) -> Interface:
    iface = Interface(files(__package__).joinpath("io.pipanel.app.Music.varlink")
                      .read_text(encoding="utf-8"))

    @iface.method("GetStatus")
    async def get_status(call: Call) -> dict:
        return on_main(player.status) or {}

    @iface.method("Seek")
    async def seek(call: Call) -> None:
        position = call.param("position", (int, float))
        if not on_main(lambda: player.now.has_track and player.now.can_seek):
            raise VarlinkError("io.pipanel.app.Music.NotSeekable", {})
        on_main(player.command, SEEK, max(0.0, float(position)))

    @iface.method("Reload")
    async def reload(call: Call) -> None:
        error = on_main(player.reload)
        if error:
            raise VarlinkError("io.pipanel.app.Music.InvalidConfig", {"message": error})

    return iface


def parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in text.lower().split("x"))
        if width < 320 or height < 240:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT, e.g. 1920x1080") from None
    return width, height


def main() -> int:
    p = argparse.ArgumentParser(prog="pi-panel-music")
    p.add_argument("--config", type=Path, help="config.toml (not needed with --demo)")
    p.add_argument("--log-level", default="INFO")
    dev = p.add_argument_group("for working on the screen")
    dev.add_argument("--demo", nargs="?", const=PLAYING, choices=(PLAYING, PAUSED, IDLE),
                     help="a pretend player instead of a backend (optionally: its state)")
    dev.add_argument("--demo-track", type=int, default=0, metavar="N",
                     help="which of the demo's tracks to start on")
    dev.add_argument("--layout", choices=LAYOUTS, help="override [display] layout")
    dev.add_argument("--size", type=parse_size, metavar="WxH",
                     help="lay out for this screen size instead of asking the display")
    dev.add_argument("--screenshot", type=Path, metavar="PNG",
                     help="save the window as a PNG after a few seconds, and exit")
    args = p.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)-7s %(message)s", stream=sys.stderr)
    # asyncio.CancelledError noise from websockets' keepalive at teardown.
    logging.getLogger("websockets").setLevel(logging.WARNING)

    if args.demo:
        config = Config(backend="music_assistant")
        if args.config:
            p.error("--demo takes no --config")
    elif not args.config:
        p.error("--config is required")
    else:
        try:
            config = Config.load(args.config)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2

    from . import ui  # noqa: F401 - pins the GTK versions before anything imports them
    from gi.repository import GLib, Gtk

    # The Wayland app_id; pi-panel matches the window by its unit, but this
    # is what `pi-panel-ctl` output shows.
    GLib.set_prgname(os.environ.get("PI_PANEL_NAME", "music"))
    if not Gtk.init_check():
        print("cannot open the display (is WAYLAND_DISPLAY set?)", file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    player = Player(args, config, loop)

    def idle(fn, *a) -> None:
        GLib.idle_add(lambda: fn(*a) and False)

    def on_main(fn, *a) -> Any:
        """Hand a call from the service thread to the GLib loop, and wait for it."""
        done = threading.Event()
        result: list[Any] = []

        def run() -> bool:
            try:
                result.append(fn(*a))
            finally:
                done.set()
            return False
        GLib.idle_add(run)
        done.wait(5)
        return result[0] if result else None

    service = AppService(
        product="pi-panel-app-music", version=VERSION,
        on_visible=lambda visible: idle(player.set_visible, visible),
        actions={
            "play_pause": ("Play / pause", lambda: idle(player.command, PLAY_PAUSE)),
            "next": ("Next track", lambda: idle(player.command, NEXT)),
            "previous": ("Previous track", lambda: idle(player.command, PREVIOUS)),
            "reload": ("Reload config", lambda: idle(player.reload)),
        },
        interfaces=[music_interface(player, on_main)],
    )
    if not service.start():
        log.info("not running under pi-panel: always visible")
        player.set_visible(True)

    # SIGTERM from systemd: leave the loop, then tear down; well inside the
    # app contract's 5 seconds.
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: loop.quit() or False)
    # SIGHUP: re-read config.toml (`systemctl kill -s HUP pi-panel-app@music`).
    if not args.demo:
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP,
                             lambda: (player.reload(), True)[1])
    player.window.connect("close-request", lambda _w: loop.quit() or False)

    player.start()
    if args.screenshot:
        from .ui.screenshot import save
        GLib.timeout_add(4000, lambda: (save(player.window, args.screenshot), loop.quit(), False)[-1])

    loop.run()
    log.info("stopping")
    player.shutdown()
    service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
