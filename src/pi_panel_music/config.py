"""config.toml: the backend, when to show the app, and the layout."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BACKENDS = ("music_assistant", "home_assistant")
LAYOUTS = ("side", "stacked")
SIDES = ("left", "right")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MusicAssistant:
    url: str
    token: str = ""
    player: str = ""      # name or id; empty follows whichever is playing


@dataclass(frozen=True, slots=True)
class HomeAssistant:
    url: str
    token: str
    entity: str


@dataclass(frozen=True, slots=True)
class Show:
    auto: bool = True
    priority: int = 10
    release_after: float = 30.0


@dataclass(frozen=True, slots=True)
class Display:
    layout: str = "side"
    artwork_side: str = "left"


@dataclass(frozen=True, slots=True)
class Config:
    backend: str
    music_assistant: MusicAssistant | None = None
    home_assistant: HomeAssistant | None = None
    show: Show = field(default_factory=Show)
    display: Display = field(default_factory=Display)

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError(f"{path} does not exist (copy config.toml.example)") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from None
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        _only(data, "config", {"backend", "music_assistant", "home_assistant", "show", "display"})
        backend = data.get("backend")
        if backend not in BACKENDS:
            raise ConfigError(f"backend must be one of: {', '.join(BACKENDS)}")

        # Only the selected backend's section has to be complete: the other
        # one is usually the untouched example.
        mass = hass = None
        if backend == "music_assistant":
            t = _table(data, "music_assistant", {"url", "token", "player"}, required=True)
            mass = MusicAssistant(url=_url(t, "music_assistant"),
                                  token=_str(t, "music_assistant", "token"),
                                  player=_str(t, "music_assistant", "player"))
        else:
            t = _table(data, "home_assistant", {"url", "token", "entity"}, required=True)
            token = _str(t, "home_assistant", "token")
            if not token:
                raise ConfigError("home_assistant: token is required (a long-lived access token)")
            entity = _str(t, "home_assistant", "entity")
            if not entity.startswith("media_player.") or len(entity) <= len("media_player."):
                raise ConfigError("home_assistant: entity must be a media_player.* entity id")
            hass = HomeAssistant(url=_url(t, "home_assistant"), token=token, entity=entity)

        t = _table(data, "show", {"auto", "priority", "release_after"})
        auto = t.get("auto", True)
        if not isinstance(auto, bool):
            raise ConfigError("show: auto must be true or false")
        priority = t.get("priority", 10)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ConfigError("show: priority must be a whole number")
        release = t.get("release_after", 30)
        if isinstance(release, bool) or not isinstance(release, (int, float)) or release < 0:
            raise ConfigError("show: release_after must be a number >= 0")

        t = _table(data, "display", {"layout", "artwork_side"})
        layout = t.get("layout", "side")
        if layout not in LAYOUTS:
            raise ConfigError(f"display: layout must be one of: {', '.join(LAYOUTS)}")
        side = t.get("artwork_side", "left")
        if side not in SIDES:
            raise ConfigError(f"display: artwork_side must be one of: {', '.join(SIDES)}")

        return cls(backend=backend, music_assistant=mass, home_assistant=hass,
                   show=Show(auto, priority, float(release)),
                   display=Display(layout, side))


def _only(table: dict[str, Any], where: str, known: set[str]) -> None:
    unknown = set(table) - known
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")


def _table(data: dict[str, Any], name: str, known: set[str], required: bool = False) -> dict:
    table = data.get(name)
    if table is None:
        if required:
            raise ConfigError(f"backend = \"{name}\" needs a [{name}] section")
        return {}
    if not isinstance(table, dict):
        raise ConfigError(f"{name} must be a [{name}] section")
    _only(table, name, known)
    return table


def _str(table: dict[str, Any], where: str, key: str) -> str:
    value = table.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be a string")
    return value.strip()


def _url(table: dict[str, Any], where: str) -> str:
    url = _str(table, where, "url").rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{where}: url must be an http:// or https:// address")
    return url
