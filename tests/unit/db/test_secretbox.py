import logging
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from mixsync.core.config import Settings
from mixsync.db.secretbox import SecretBox, load_box


def _settings(tmp_path: Path, key: str = "") -> Settings:
    return Settings(config_dir=str(tmp_path), secret_key=key)


def test_round_trip_and_wrong_key() -> None:
    box = SecretBox(Fernet.generate_key())
    token = box.encrypt("hunter2")
    assert "hunter2" not in token and box.decrypt(token) == "hunter2"
    assert SecretBox(Fernet.generate_key()).decrypt(token) is None


def test_generates_key_file_mode_600_and_reuses_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        first = load_box(_settings(tmp_path))
    assert "back up this file" in caplog.text
    assert (tmp_path / "secret.key").stat().st_mode & 0o777 == 0o600
    assert load_box(_settings(tmp_path)).decrypt(first.encrypt("x")) == "x"


def test_refuses_key_file_readable_by_others(tmp_path: Path) -> None:
    f = tmp_path / "secret.key"
    f.write_bytes(Fernet.generate_key())
    os.chmod(f, 0o644)
    with pytest.raises(RuntimeError, match="readable by others"):
        load_box(_settings(tmp_path))


def test_env_key_wins_and_warns_when_file_differs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    f = tmp_path / "secret.key"
    f.write_bytes(Fernet.generate_key())
    os.chmod(f, 0o600)
    env = Fernet.generate_key().decode()
    with caplog.at_level(logging.WARNING):
        box = load_box(_settings(tmp_path, env))
    assert "differs" in caplog.text and env not in caplog.text
    assert SecretBox(env.encode()).decrypt(box.encrypt("x")) == "x"


def test_env_key_alone_writes_no_file(tmp_path: Path) -> None:
    load_box(_settings(tmp_path, Fernet.generate_key().decode()))
    assert not (tmp_path / "secret.key").exists()
