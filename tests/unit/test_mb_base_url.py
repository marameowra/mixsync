from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync import wiring, worker
from mixsync.core.config import Settings
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.web import deps
from mixsync.wiring import build_services

MIRROR = "http://mirror.test:5000"


class Recorder(MusicBrainzProvider):
    urls: list[str] = []

    def __init__(self, client: Any, base_url: str = "https://musicbrainz.org") -> None:
        Recorder.urls.append(base_url)
        super().__init__(client, base_url)


def _worker(services: Any) -> None:
    worker._new_queue(services)  # pyright: ignore[reportPrivateUsage]


def _web_watch(services: Any) -> None:
    state = SimpleNamespace(services=services)
    deps.get_watch(SimpleNamespace(app=SimpleNamespace(state=state)))  # type: ignore[arg-type]


def _web_merger(services: Any) -> None:
    state = SimpleNamespace(services=services)
    deps.get_merger(SimpleNamespace(app=SimpleNamespace(state=state)))  # type: ignore[arg-type]


@pytest.mark.parametrize("consumer", [_worker, _web_watch, _web_merger])
def test_musicbrainz_is_built_on_the_configured_base_url(
    consumer: Callable[[Any], None],
    settings: Settings,
    sessions: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("CONFIG", "DATA", "DOWNLOADS"):
        (tmp_path / var).mkdir()
        monkeypatch.setenv(f"MIXSYNC_{var}_DIR", str(tmp_path / var))
    monkeypatch.setenv("MIXSYNC_MB_BASE_URL", MIRROR)
    Recorder.urls = []
    monkeypatch.setattr(wiring, "MusicBrainzProvider", Recorder)
    consumer(build_services(Settings(), sessions))
    assert Recorder.urls and set(Recorder.urls) == {MIRROR}
