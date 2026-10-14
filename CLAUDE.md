# pi-panel-app-music

A pi-panel app: an Apple Music-style now-playing screen, fed by Music
Assistant or a Home Assistant media_player. GTK 4 through PyGObject. The
README covers usage.

```bash
uv run pytest      # offline; GTK isn't needed for these
```

## The config holds a token

It lives in `~/.config/pi-panel/apps/music/config.toml` on the Pi. A local
`config.toml` is gitignored. Do not commit it, print it, or paste it into a
log or a message. Home Assistant's artwork URLs carry a token too, which is
why `artwork.py` never logs a URL.

## Working on the screen

The dev machine has no GTK, so the screen is run on `pi@jazz` (see
`../../pi-panel-core/CLAUDE.md` for jazz). Without touching the live panel:

```bash
rsync -a --delete --exclude .git --exclude .venv --exclude __pycache__ ./ pi@jazz:pi-panel-dev/music/
# on jazz: a headless compositor, then the app as the unit its slot matches
systemd-run --user --unit pp-dev -E WLR_BACKENDS=headless -E WLR_RENDERER=gles2 \
    ~/pi-panel-core/pi-panel-core-compositor/build/pi-panel-compositor \
    --ipc-socket /tmp/pp-dev.varlink --wayland-socket pp-dev-0
varlinkctl call /tmp/pp-dev.varlink io.pipanel.Compositor.RegisterSlot '{"name":"musicdev"}'
systemd-run --user --wait --unit pi-panel-app@musicdev -E WAYLAND_DISPLAY=pp-dev-0 \
    -E GTK_A11Y=none -E GDK_DISABLE=vulkan --working-directory=$HOME/pi-panel-dev/music \
    ~/pi-panel-dev/music/.venv/bin/pi-panel-music --demo --screenshot ~/pi-panel-dev/shot.png
```

- `--screenshot` renders the window through GTK itself, so no screenshot tool is needed. The headless output is 1280×720; the layout scales to it.
- The dev directory must sit two levels below the home directory, like an installed app, for the relative path to pi-panel-varlink to resolve.
- Add `-E PI_PANEL_APP_SOCKET=/tmp/x.sock` to exercise the Varlink service with `varlinkctl`.

Measured on jazz with `--demo`: about 0.5% of one core while playing and
visible, nothing while hidden, about 145 MB resident, and 0.1s to stop.

## Design points worth preserving

- **PyGObject comes from apt; everything else from uv.** The venv is created with `--system-site-packages` so it sees the system `gi`, which must match the system GTK. Don't add PyGObject to `pyproject.toml`: pip would compile it on the Pi.
- **Only `ui/` and `artwork.py` import GTK.** Config, model, backends and the show/release policy are plain Python and tested offline. `ui/__init__.py` pins the GTK versions; anything that imports `gi.repository` must import `ui` first.
- **Three threads, one owner.** All state lives on the GLib loop. The backend and pi-panel-ctl calls run on the network thread's asyncio loop (`net.py`); the io.pipanel.App service on its own. Both hand over with `GLib.idle_add`. Backend callbacks are tagged with their backend object, so one that a reload replaced can't touch the screen.
- **Nothing polls.** Both backends are pushed changes. The position on screen is worked out from the last reported position and its timestamp (`NowPlaying.position_now`). The only timer is 4 Hz, runs only while visible and playing, and redraws only when the line would move half a pixel.
- **The blur is rendered once per cover.** `artwork.make_backdrop` renders the blur into a 160px texture with the window's own renderer; the backdrop then just stretches it. Blurring live (a CSS filter, or a blur node in the widget) would be redone on every frame that anything changes.
- **The widgets paint themselves** (`ui/widgets.py`, `do_snapshot`). Icons are path data, not an icon theme or SVG files, so there is no dependency on librsvg. Their subpaths all run clockwise: with the winding fill rule, overlapping subpaths drawn in opposite directions cut holes.
- **Labels ask for almost no width** (`width_chars = max_width_chars = 1`) and are given the column's. Otherwise a long title makes the column, and so the whole layout, wider.
- **Artwork threads are daemon threads**, and a cover that is still loading leaves the old one up. A slow image server must never hold up SIGTERM (the app contract allows 5s).
- **At most one hold, and never none while playing.** Pressing play asks again on purpose: `pi-panel-ctl next` and `switch` (Home Assistant's Showing select) drop holds without telling the app. A track change does not ask: players stop for up to a second between tracks, so a stop shorter than `RESUME_GAP` (2s) is ignored, or music would undo the user's pick at the end of every song. `PanelHold.show` asks for the new hold first and drops the old one after. The other order leaves a moment with no hold, and ctl fades to the next app and back. That happened at every track change, because players report a brief pause between tracks. Overlapping calls still end with one hold, and a `release()` during a pending `Show` drops what it returns (`wanted`).
- **On a lost connection the track is cleared**, not left frozen: the app then releases the screen through the normal pause path.
- **`GDK_DISABLE=vulkan`, `GTK_A11Y=none`** in the manifest: without them GTK logs a Vulkan failure on every start and waits for an accessibility bus the unit doesn't have. No Gtk.Application either: it wants a session bus.

## Not yet verified

- **Against real servers.** The two protocols are implemented from Home Assistant's WebSocket API and the official Music Assistant client's source, and tested against fakes. Field names (especially Music Assistant's queue item and image shapes, and whether `/imageproxy` wants the token on schema 28+) need checking against a live server.
- **Touch.** jazz has no touchscreen, and the compositor's touch forwarding has never run on hardware. The buttons and scrubber use ordinary GTK gestures.
- **The compositor does not route the pointer to apps,** so a mouse can't press the buttons.
