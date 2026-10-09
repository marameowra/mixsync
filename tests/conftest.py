import socket
from pathlib import Path

import pytest

from mixsync.core.config import Settings


@pytest.fixture(autouse=True)
def _block_sockets(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    def blocked(*_a: object, **_k: object) -> None:
        raise RuntimeError("network access is blocked in unit tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MIXSYNC_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    return Settings()
