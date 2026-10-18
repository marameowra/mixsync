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


def test_worker_builds_musicbrainz_on_the_configured_base_url(
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
    worker._new_queue(build_services(Settings()))  # pyright: ignore[reportPrivateUsage]
    assert Recorder.urls and set(Recorder.urls) == {MIRROR}


def test_web_builds_musicbrainz_on_the_configured_base_url(
    settings: Settings, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIXSYNC_MB_BASE_URL", MIRROR)
    Recorder.urls = []
    monkeypatch.setattr(wiring, "MusicBrainzProvider", Recorder)
    state = SimpleNamespace(services=build_services(Settings(), sessions))
    deps.get_watch(SimpleNamespace(app=SimpleNamespace(state=state)))  # type: ignore[arg-type]
    assert Recorder.urls == [MIRROR]


def test_web_merger_builds_musicbrainz_on_the_configured_base_url(
    settings: Settings, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIXSYNC_MB_BASE_URL", MIRROR)
    Recorder.urls = []
    monkeypatch.setattr(wiring, "MusicBrainzProvider", Recorder)
    state = SimpleNamespace(services=build_services(Settings(), sessions))
    deps.get_merger(SimpleNamespace(app=SimpleNamespace(state=state)))  # type: ignore[arg-type]
    assert Recorder.urls == [MIRROR]
