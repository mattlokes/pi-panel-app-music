"""Where "now playing" comes from. Each backend is an asyncio task on the
network thread; see base.Backend."""

from __future__ import annotations

from ..config import Config
from .base import Backend, BackendError, OnStatus, OnUpdate

__all__ = ["Backend", "BackendError", "create"]


def create(config: Config, on_update: OnUpdate, on_status: OnStatus) -> Backend:
    if config.backend == "music_assistant":
        from .music_assistant import MusicAssistantBackend
        assert config.music_assistant is not None
        return MusicAssistantBackend(config.music_assistant, on_update, on_status)
    from .home_assistant import HomeAssistantBackend
    assert config.home_assistant is not None
    return HomeAssistantBackend(config.home_assistant, on_update, on_status)
