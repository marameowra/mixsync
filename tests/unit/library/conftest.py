import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import SystemClock
from mixsync.core.config import Settings
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.migrate import upgrade
from mixsync.library.fileops import FileOps

AUDIO = Path(__file__).parents[2] / "fixtures" / "audio"
FORMATS = ["flac", "mp3", "m4a", "ogg"]

MakeSrc = Callable[..., Path]


@pytest.fixture
def sessions(settings: Settings) -> sessionmaker[Session]:
    upgrade()
    return make_session_factory(make_engine(settings))


@pytest.fixture
def journal(sessions: sessionmaker[Session]) -> SqlJournal:
    return SqlJournal(sessions, SystemClock())


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "library").mkdir(parents=True)
    return d


@pytest.fixture
def ops(data_dir: Path, journal: SqlJournal) -> FileOps:
    return FileOps(data_dir, journal, SystemClock())


@pytest.fixture
def make_src(tmp_path: Path) -> MakeSrc:
    def make(ext: str = "flac", name: str = "src") -> Path:
        src = tmp_path / "downloads" / f"{name}.{ext}"
        src.parent.mkdir(exist_ok=True)
        shutil.copy(AUDIO / f"cc0-tone-01.{ext}", src)
        return src

    return make
