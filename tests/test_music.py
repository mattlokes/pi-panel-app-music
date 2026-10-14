"""Offline tests: config, the two backends, and the show/release policy.

No GTK needed: the screen is exercised on the Pi (see CLAUDE.md). The
backends are run for real against small fake servers on localhost.
Run with:  uv run pytest
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from pi_panel_music.autoshow import AutoShow
from pi_panel_music.backends import create
from pi_panel_music.backends.base import NEXT, PLAY_PAUSE, SEEK, websocket_url
from pi_panel_music.backends.demo import DemoBackend, artwork_png
from pi_panel_music.backends.home_assistant import apply_event, parse_entity
from pi_panel_music.backends.music_assistant import image_url, parse_queue, select_queue
from pi_panel_music.config import Config, ConfigError
from pi_panel_music.model import IDLE, PAUSED, PLAYING, NowPlaying, clock_text, time_labels

ROOT = Path(__file__).resolve().parent.parent
MASS = {"url": "http://mass.local:8095"}
HASS = {"url": "http://hass.local:8123", "token": "t", "entity": "media_player.kitchen"}


# --- config -------------------------------------------------------------------

def test_shipped_example_is_valid():
    cfg = Config.load(ROOT / "config.toml.example")
    assert cfg.backend == "music_assistant"
    assert cfg.music_assistant.url == "http://mass.home.local:8095"
    assert cfg.home_assistant is None          # the unselected backend isn't read
    assert (cfg.show.auto, cfg.show.priority, cfg.show.release_after) == (True, 10, 30.0)
    assert (cfg.display.layout, cfg.display.artwork_side) == ("side", "left")


def test_only_the_selected_backend_must_be_complete():
    cfg = Config.from_dict({"backend": "home_assistant", "home_assistant": HASS,
                            "music_assistant": {"url": ""}})
    assert cfg.home_assistant.entity == "media_player.kitchen"
    assert cfg.music_assistant is None


def test_url_loses_its_trailing_slash():
    cfg = Config.from_dict({"backend": "music_assistant",
                            "music_assistant": {"url": "http://mass.local:8095/"}})
    assert cfg.music_assistant.url == "http://mass.local:8095"


@pytest.mark.parametrize("data,msg", [
    ({}, "backend must be one of"),
    ({"backend": "spotify"}, "backend must be one of"),
    ({"backend": "music_assistant"}, r"needs a \[music_assistant\] section"),
    ({"backend": "music_assistant", "music_assistant": {"url": "mass.local"}}, "http://"),
    ({"backend": "music_assistant", "music_assistant": {**MASS, "user": "x"}}, "unknown"),
    ({"backend": "home_assistant", "home_assistant": {**HASS, "token": ""}}, "token is required"),
    ({"backend": "home_assistant", "home_assistant": {**HASS, "entity": "light.x"}}, "media_player"),
    ({"backend": "music_assistant", "music_assistant": MASS, "show": {"auto": "yes"}}, "auto"),
    ({"backend": "music_assistant", "music_assistant": MASS, "show": {"priority": 1.5}}, "priority"),
    ({"backend": "music_assistant", "music_assistant": MASS, "show": {"release_after": -1}},
     "release_after"),
    ({"backend": "music_assistant", "music_assistant": MASS, "display": {"layout": "tall"}},
     "layout"),
    ({"backend": "music_assistant", "music_assistant": MASS, "display": {"artwork_side": "top"}},
     "artwork_side"),
    ({"backend": "music_assistant", "music_assistant": MASS, "extra": 1}, "unknown"),
])
def test_config_rejects(data, msg):
    with pytest.raises(ConfigError, match=msg):
        Config.from_dict(data)


def test_manifest_parses():
    data = tomllib.loads((ROOT / "pi-panel.toml").read_text())
    assert data["package"]["name"] == "music"
    assert data["app"] == {"varlink": True, "rotate": False}
    assert data["config"]["templates"] == ["config.toml.example"]


def test_interface_file_is_valid_idl():
    if not shutil.which("varlinkctl"):
        pytest.skip("varlinkctl not installed")
    path = ROOT / "src/pi_panel_music/io.pipanel.app.Music.varlink"
    subprocess.run(["varlinkctl", "validate-idl", str(path)], check=True, capture_output=True)


# --- model --------------------------------------------------------------------

def test_position_advances_only_while_playing_and_stops_at_the_end():
    playing = NowPlaying(state=PLAYING, title="t", duration=200.0, position=50.0, position_at=100.0)
    assert playing.position_now(110.0) == 60.0
    assert playing.position_now(1000.0) == 200.0
    assert playing.fraction(110.0) == pytest.approx(0.3)
    paused = NowPlaying(state=PAUSED, title="t", duration=200.0, position=50.0, position_at=100.0)
    assert paused.position_now(110.0) == 50.0
    assert NowPlaying().position_now(5.0) is None
    assert NowPlaying().fraction(5.0) == 0.0


def test_time_labels():
    assert [clock_text(s) for s in (0, 65, 760, 3723)] == ["0:00", "1:05", "12:40", "1:02:03"]
    track = NowPlaying(state=PAUSED, title="t", duration=225.0, position=65.0, position_at=0.0)
    assert time_labels(track, 0.0) == ("1:05", "\u22122:40")
    assert time_labels(track, 0.0, position=10.0) == ("0:10", "\u22123:35")      # scrubbing
    stream = NowPlaying(state=PAUSED, title="radio", position=65.0)
    assert time_labels(stream, 0.0) == ("1:05", "")
    assert time_labels(NowPlaying(), 0.0) == ("", "")


# --- Home Assistant -----------------------------------------------------------

ENTITY = "media_player.kitchen"


def test_entity_events_add_change_and_remove():
    entity = apply_event(ENTITY, None, {"a": {ENTITY: {"s": "paused", "a": {"media_title": "A",
                                                                             "volume_level": 0.3}}}})
    assert entity == {"s": "paused", "a": {"media_title": "A", "volume_level": 0.3}}
    changed = apply_event(ENTITY, entity, {"c": {ENTITY: {
        "+": {"s": "playing", "a": {"media_title": "B"}}, "-": {"a": ["volume_level"]}}}})
    assert changed == {"s": "playing", "a": {"media_title": "B"}}
    assert entity["a"]["media_title"] == "A"                # the input isn't modified
    assert apply_event(ENTITY, changed, {"c": {"media_player.other": {"+": {"s": "off"}}}}) == changed
    assert apply_event(ENTITY, changed, {"r": [ENTITY]}) is None
    assert apply_event(ENTITY, None, {"a": {}}) is None      # the entity doesn't exist


def test_parse_entity():
    stamp = datetime(2026, 10, 13, 20, 0, 0, tzinfo=timezone.utc)
    entity = {"s": "playing", "a": {
        "media_title": "Low Tide", "media_artist": "Marlowe", "media_album_name": "Harbour Lights",
        "media_duration": 181, "media_position": 40,
        "media_position_updated_at": stamp.isoformat(),
        "entity_picture": "/api/media_player_proxy/media_player.kitchen?token=abc&cache=1",
        "supported_features": 2 | 16 | 32 | 1,
    }}
    # 10s after the stamp on the wall clock is 500.0 on the monotonic one.
    now = parse_entity(entity, "http://hass.local:8123", stamp.timestamp() + 10, 500.0)
    assert (now.state, now.title, now.artist, now.album) == (
        PLAYING, "Low Tide", "Marlowe", "Harbour Lights")
    assert now.artwork_url == \
        "http://hass.local:8123/api/media_player_proxy/media_player.kitchen?token=abc&cache=1"
    assert (now.duration, now.position, now.position_at) == (181.0, 40.0, 490.0)
    assert now.position_now(500.0) == 50.0
    assert (now.can_seek, now.can_next, now.can_previous) == (True, True, True)


def test_parse_entity_edge_cases():
    base = "http://hass.local:8123"
    for state in ("idle", "off", "standby", "unavailable", "unknown", None):
        assert parse_entity({"s": state, "a": {"media_title": "old"}}, base, 0, 0) == NowPlaying()
    assert parse_entity(None, base, 0, 0) == NowPlaying()
    bare = parse_entity({"s": "paused", "a": {
        "media_title": "Radio 4", "entity_picture": "https://cdn.example/x.jpg",
        "media_position": 5}}, base, 0, 77.0)
    assert bare.state == PAUSED and bare.artwork_url == "https://cdn.example/x.jpg"
    assert bare.duration is None and bare.position_at == 77.0
    assert (bare.can_seek, bare.can_next, bare.can_previous) == (False, False, False)
    assert parse_entity({"s": "buffering", "a": {}}, base, 0, 0).state == PLAYING


# --- Music Assistant ----------------------------------------------------------

def queue(queue_id="kitchen", name="Kitchen", state="playing", **extra):
    return {
        "queue_id": queue_id, "display_name": name, "state": state, "items": 12, "current_index": 3,
        "elapsed_time": 40.0, "elapsed_time_last_updated": 1000.0, "repeat_mode": "off",
        "current_item": {
            "queue_item_id": "q1", "name": "Marlowe - Low Tide", "duration": 181,
            "image": {"type": "thumb", "path": "/library/art/1.jpg", "provider": "filesystem",
                      "remotely_accessible": False},
            "media_item": {"name": "Low Tide", "artists": [{"name": "Marlowe"}, {"name": "Guest"}],
                           "album": {"name": "Harbour Lights"}},
        },
        **extra,
    }


def test_select_queue():
    queues = {"a": queue("a", "Kitchen", "idle"), "b": queue("b", "Living Room", "playing"),
              "c": queue("c", "Office", "paused")}
    assert select_queue(queues, "living room", None) == "b"       # by name, any case
    assert select_queue(queues, "c", None) == "c"                 # by id
    assert select_queue(queues, "Garage", "b") is None
    assert select_queue(queues, "", None) == "b"                  # whichever is playing
    queues["c"]["state"] = "playing"
    assert select_queue(queues, "", "b") == "b"                   # stays while it still is
    queues["b"]["state"] = "paused"
    assert select_queue(queues, "", "b") == "c"                   # moves to the one playing
    queues["c"]["state"] = "idle"
    assert select_queue(queues, "", "c") == "c"                   # nothing playing: stay put
    assert select_queue(queues, "", None) == "b"                  # ...or a paused one
    assert select_queue({}, "", None) is None


def test_parse_queue():
    now = parse_queue(queue(), "http://mass.local:8095", 27, wall=1010.0, monotonic=500.0)
    assert (now.state, now.title, now.artist, now.album) == (
        PLAYING, "Low Tide", "Marlowe, Guest", "Harbour Lights")
    assert (now.duration, now.position, now.position_at) == (181.0, 40.0, 490.0)
    assert now.artwork_url == ("http://mass.local:8095/imageproxy?path=/library/art/1.jpg"
                               "&provider=filesystem&size=1024")
    assert (now.can_seek, now.can_next, now.can_previous) == (True, True, True)


def test_parse_queue_edge_cases():
    base = "http://mass.local:8095"
    assert parse_queue(None, base, 27, 0, 0) == NowPlaying()
    assert parse_queue(queue(state="idle"), base, 27, 0, 0) == NowPlaying()
    assert parse_queue(queue(current_item=None), base, 27, 0, 0) == NowPlaying()
    radio = parse_queue(queue(state="paused", current_item={
        "name": "Nina Simone - Sinnerman", "duration": 0, "media_item": None}), base, 27, 0, 0)
    assert (radio.state, radio.artist, radio.title) == (PAUSED, "Nina Simone", "Sinnerman")
    assert radio.duration is None and not radio.can_seek and radio.artwork_url is None
    assert not parse_queue(queue(items=4, current_index=3), base, 27, 0, 0).can_next
    assert parse_queue(queue(items=4, current_index=3, repeat_mode="all"), base, 27, 0, 0).can_next


def test_image_url():
    base = "http://mass.local:8095"
    remote = {"path": "https://cdn.example/a b.jpg", "provider": "spotify", "remotely_accessible": True}
    assert image_url(remote, base, 27) == "https://cdn.example/a b.jpg"
    local = {"path": "/music/a b/cover.jpg", "provider": "filesystem--x", "remotely_accessible": False}
    assert image_url(local, base, 27) == (
        base + "/imageproxy?path=/music/a%2520b/cover.jpg&provider=filesystem--x&size=1024")
    assert image_url({**local, "proxy_id": "abc123"}, base, 31) == base + "/imageproxy/abc123?size=1024"
    assert image_url({**local, "proxy_id": "abc123"}, base, 30).startswith(base + "/imageproxy?path=")
    assert image_url(None, base, 27) is None and image_url({"path": ""}, base, 27) is None


# --- the backends, against fake servers ------------------------------------------

class Recorder:
    """What a backend reported, and a way to wait for it."""

    def __init__(self):
        self.updates: list[NowPlaying] = []
        self.statuses: list[tuple[bool, str]] = []
        self.event = asyncio.Event()

    def on_update(self, now_playing):
        self.updates.append(now_playing)
        self.event.set()

    def on_status(self, connected, problem):
        self.statuses.append((connected, problem))
        self.event.set()

    async def until(self, condition):
        async with asyncio.timeout(5):
            while not condition():
                self.event.clear()
                await self.event.wait()


async def run_against(handler, config_for, scenario):
    """Serve |handler| on localhost, run the configured backend against it,
    and play out |scenario(backend, recorder)|."""
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        recorder = Recorder()
        backend = create(Config.from_dict(config_for(f"http://127.0.0.1:{port}")),
                         recorder.on_update, recorder.on_status)
        task = asyncio.create_task(backend.run())
        try:
            await scenario(backend, recorder)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_home_assistant_backend():
    received: list[dict] = []

    async def hass(ws):
        assert ws.request.path == "/api/websocket"
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        if auth != {"type": "auth", "access_token": "secret"}:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        subscribe = json.loads(await ws.recv())
        assert subscribe["type"] == "subscribe_entities" and subscribe["entity_ids"] == [ENTITY]
        sid = subscribe["id"]
        await ws.send(json.dumps({"id": sid, "type": "result", "success": True}))
        await ws.send(json.dumps({"id": sid, "type": "event", "event": {"a": {ENTITY: {
            "s": "paused", "a": {"media_title": "Low Tide", "media_artist": "Marlowe",
                                 "media_duration": 181, "supported_features": 2 | 32}}}}}))
        async for raw in ws:
            message = json.loads(raw)
            received.append(message)
            await ws.send(json.dumps({"id": message["id"], "type": "result", "success": True}))
            if message["service"] == "media_play_pause":
                await ws.send(json.dumps({"id": sid, "type": "event", "event": {
                    "c": {ENTITY: {"+": {"s": "playing"}}}}}))

    async def scenario(backend, seen):
        await seen.until(lambda: seen.updates and seen.updates[-1].state == PAUSED)
        assert seen.statuses == [(True, "")]
        assert seen.updates[-1].title == "Low Tide" and seen.updates[-1].can_next
        await backend.command(PLAY_PAUSE)
        await seen.until(lambda: seen.updates[-1].state == PLAYING)
        assert seen.updates[-1].title == "Low Tide"             # kept across the diff
        await backend.command(SEEK, 42.5)
        await backend.command(NEXT)
        async with asyncio.timeout(5):
            while len(received) < 3:
                await asyncio.sleep(0.01)
        assert [m["service"] for m in received] == [
            "media_play_pause", "media_seek", "media_next_track"]
        assert all(m["type"] == "call_service" and m["domain"] == "media_player"
                   and m["target"] == {"entity_id": ENTITY} for m in received)
        assert received[1]["service_data"] == {"seek_position": 42.5}
        assert len({m["id"] for m in received}) == 3

    asyncio.run(run_against(hass, lambda url: {
        "backend": "home_assistant",
        "home_assistant": {"url": url, "token": "secret", "entity": ENTITY}}, scenario))


def test_home_assistant_bad_token_and_missing_entity():
    async def hass(ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        if auth["access_token"] != "secret":
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        sid = json.loads(await ws.recv())["id"]
        await ws.send(json.dumps({"id": sid, "type": "event", "event": {"a": {}}}))
        await ws.wait_closed()

    async def refused(backend, seen):
        await seen.until(lambda: bool(seen.statuses))
        assert seen.statuses[0] == (False, "Home Assistant refused the access token")
        assert seen.updates[-1] == NowPlaying()

    async def missing(backend, seen):
        await seen.until(lambda: bool(seen.statuses))
        assert seen.statuses[0] == (True, f"{ENTITY} does not exist")

    for token, scenario in (("wrong", refused), ("secret", missing)):
        asyncio.run(run_against(hass, lambda url, token=token: {
            "backend": "home_assistant",
            "home_assistant": {"url": url, "token": token, "entity": ENTITY}}, scenario))


def music_assistant_server(received: list[dict], schema: int = 28):
    async def mass(ws):
        assert ws.request.path == "/ws"
        await ws.send(json.dumps({"server_id": "x", "server_version": "2.7.0",
                                  "schema_version": schema}))
        async for raw in ws:
            message = json.loads(raw)
            received.append(message)
            mid, command = message["message_id"], message["command"]
            if command == "auth":
                if message["args"].get("token") != "secret":
                    await ws.send(json.dumps({"message_id": mid, "error_code": 20,
                                              "details": "Invalid token"}))
                    continue
                await ws.send(json.dumps({"message_id": mid, "result": {"user": "pi"}}))
            elif command == "player_queues/all":
                # An event arriving before the reply must not be taken for it.
                await ws.send(json.dumps({"event": "queue_time_updated", "object_id": "kitchen",
                                          "data": 1}))
                await ws.send(json.dumps({"message_id": mid, "result": [
                    queue("kitchen", "Kitchen", "idle"), queue("office", "Office", "paused")]}))
            elif command == "player_queues/play_pause":
                await ws.send(json.dumps({"message_id": mid, "result": None}))
                await ws.send(json.dumps({
                    "event": "queue_updated", "object_id": message["args"]["queue_id"],
                    "data": queue("office", "Office", "playing")}))
                await ws.send(json.dumps({"event": "queue_time_updated", "object_id": "office",
                                          "data": 99.0}))
            else:
                await ws.send(json.dumps({"message_id": mid, "result": None}))
    return mass


def test_music_assistant_backend():
    received: list[dict] = []

    async def scenario(backend, seen):
        await seen.until(lambda: seen.updates and seen.updates[-1].state == PAUSED)
        assert seen.statuses == [(True, "")]
        assert [m["command"] for m in received] == ["auth", "player_queues/all"]
        assert seen.updates[-1].title == "Low Tide"
        assert seen.updates[-1].artwork_url.startswith(backend.config.url + "/imageproxy?path=")
        await backend.command(PLAY_PAUSE)
        await seen.until(lambda: seen.updates[-1].state == PLAYING
                         and seen.updates[-1].position == 99.0)
        await backend.command(SEEK, 30.7)
        await backend.command(NEXT)
        async with asyncio.timeout(5):
            while len(received) < 5:
                await asyncio.sleep(0.01)
        assert [(m["command"], m["args"]) for m in received[2:]] == [
            ("player_queues/play_pause", {"queue_id": "office"}),
            ("player_queues/seek", {"queue_id": "office", "position": 30}),
            ("player_queues/next", {"queue_id": "office"}),
        ]

    asyncio.run(run_against(music_assistant_server(received), lambda url: {
        "backend": "music_assistant", "music_assistant": {"url": url, "token": "secret"}}, scenario))


@pytest.mark.parametrize("section,schema,status", [
    ({"token": "wrong"}, 28, (False, "Music Assistant refused the token")),
    ({}, 28, (False, "Music Assistant needs a token (see config.toml)")),
    ({}, 27, (True, "")),                                        # older servers have no login
    ({"token": "secret", "player": "Garage"}, 28, (True, 'no player called "Garage"')),
])
def test_music_assistant_login_and_player(section, schema, status):
    async def scenario(backend, seen):
        await seen.until(lambda: bool(seen.statuses))
        assert seen.statuses[0] == status

    asyncio.run(run_against(music_assistant_server([], schema), lambda url: {
        "backend": "music_assistant", "music_assistant": {"url": url, **section}}, scenario))


def test_backend_reconnects_after_the_server_drops_it(monkeypatch):
    monkeypatch.setattr("pi_panel_music.backends.base.BACKOFF_FIRST", 0.05)
    connections = 0

    async def hass(ws):
        nonlocal connections
        connections += 1
        await ws.send(json.dumps({"type": "auth_ok"}))
        sid = json.loads(await ws.recv())["id"]
        await ws.send(json.dumps({"id": sid, "type": "event", "event": {"a": {ENTITY: {
            "s": "playing", "a": {"media_title": f"take {connections}"}}}}}))
        if connections == 1:
            await ws.close()
        else:
            await ws.wait_closed()

    async def scenario(backend, seen):
        await seen.until(lambda: seen.updates and seen.updates[-1].title == "take 2")
        assert seen.statuses == [(True, ""), (False, "lost the connection to Home Assistant"),
                                 (True, "")]
        # In between, the stale track was cleared.
        assert [u.title for u in seen.updates] == ["take 1", "", "take 2"]

    asyncio.run(run_against(hass, lambda url: {
        "backend": "home_assistant",
        "home_assistant": {"url": url, "token": "secret", "entity": ENTITY}}, scenario))


def test_websocket_url():
    assert websocket_url("http://hass.local:8123", "/api/websocket") == \
        "ws://hass.local:8123/api/websocket"
    assert websocket_url("https://mass.example", "/ws") == "wss://mass.example/ws"


def test_demo_backend_and_its_artwork():
    async def scenario():
        seen = Recorder()
        backend = DemoBackend(seen.on_update, seen.on_status)
        task = asyncio.create_task(backend.run())
        await seen.until(lambda: bool(seen.updates))
        first = seen.updates[-1]
        assert first.playing and first.artwork_url == "demo:0"
        await backend.command(PLAY_PAUSE)
        assert seen.updates[-1].state == PAUSED
        await backend.command(NEXT)
        assert seen.updates[-1].artwork_url == "demo:1" and seen.updates[-1].position == 0.0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    png = artwork_png(1, size=16)
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82")


# --- auto-show ------------------------------------------------------------------

def make_autoshow(release_after=30.0):
    calls: list[str] = []
    return AutoShow(release_after, lambda: calls.append("show"),
                    lambda: calls.append("release")), calls


def test_autoshow_shows_on_play_and_releases_after_the_pause_outlasts_the_wait():
    auto, calls = make_autoshow()
    auto.update(False, 0.0)
    assert calls == [] and auto.deadline is None        # idle at start: nothing to give back
    auto.update(True, 1.0)
    auto.update(True, 5.0)                               # a track change is not a new start
    assert calls == ["show"]
    auto.update(False, 10.0)
    assert auto.deadline == 40.0
    auto.tick(39.9)
    assert calls == ["show"]
    auto.tick(40.0)
    assert calls == ["show", "release"] and auto.deadline is None
    auto.tick(100.0)
    assert calls == ["show", "release"]                  # only once


def test_autoshow_a_short_pause_keeps_the_screen():
    auto, calls = make_autoshow()
    auto.update(True, 0.0)
    auto.update(False, 10.0)
    auto.update(True, 20.0)                              # resumed inside the wait
    auto.tick(100.0)
    # Asked again on resume: the app may have been dismissed meanwhile.
    assert calls == ["show", "show"] and auto.deadline is None


def test_autoshow_releases_at_once_when_told_and_when_the_wait_is_zero():
    auto, calls = make_autoshow()
    auto.update(True, 0.0)
    auto.release()                                       # shutdown, or a reload
    auto.release()
    assert calls == ["show", "release"]
    auto, calls = make_autoshow(release_after=0.0)
    auto.update(True, 0.0)
    auto.update(False, 1.0)
    assert calls == ["show", "release"]


FAKE_CTL = """\
interface io.pipanel.Ctl
method Show(app: string, seconds: ?float, priority: ?int) -> (token: string)
method Release(token: string) -> ()
error NoSuchHold (token: string)
"""


def test_panel_hold_never_stacks_holds(tmp_path):
    from pi_panel_varlink import Interface, Server, VarlinkError

    from pi_panel_music.autoshow import PanelHold

    held: list[str] = []
    shows: list[dict] = []
    counts: list[int] = []                       # holds after each Show/Release
    slow = asyncio.Event()
    slow.set()
    ctl = Interface(FAKE_CTL)

    @ctl.method("Show")
    async def show(call):
        await slow.wait()
        shows.append({"app": call.param("app", str), "priority": call.param("priority", int)})
        held.append(f"token-{len(shows)}")
        counts.append(len(held))
        return {"token": held[-1]}

    @ctl.method("Release")
    async def release(call):
        token = call.param("token", str)
        if token not in held:
            raise VarlinkError("io.pipanel.Ctl.NoSuchHold", {"token": token})
        held.remove(token)
        counts.append(len(held))

    async def scenario():
        address = f"unix:{tmp_path}/ctl"
        server = Server(product="fake-ctl", version="0")
        server.add_interface(ctl)
        await server.start(address)
        try:
            hold = PanelHold(address, "music", 10)
            await hold.show()
            assert held == ["token-1"] and shows == [{"app": "music", "priority": 10}]
            counts.clear()
            await hold.show()                    # play again: the old hold is swapped, not added to
            assert held == ["token-2"]
            # The new hold came first: music was never without one, so ctl
            # had no moment to switch to another app (the track-change fade).
            assert counts == [2, 1]
            held.clear()                         # dismissed behind our back (pi-panel-ctl next)
            await hold.release()                 # NoSuchHold is not an error
            assert hold.token is None
            await asyncio.gather(hold.show(), hold.show())      # two at once
            assert held == [hold.token]
            await hold.release()
            assert held == [] and hold.token is None
            slow.clear()                         # released while a Show is on its way
            pending = asyncio.create_task(hold.show())
            await asyncio.sleep(0.05)
            await hold.release()
            slow.set()
            await pending
            assert held == [] and hold.token is None
        finally:
            await server.close()
        await hold.show()                        # ctl is down: logged, not raised
        assert hold.token is None

    asyncio.run(scenario())
