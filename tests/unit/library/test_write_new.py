from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.db.models.safety import FileOp
from mixsync.library import fileops
from mixsync.library.fileops import FileOps

REL = "A/B (2000)/cover.jpg"


def statuses(sessions: sessionmaker[Session]) -> list[str]:
    with sessions() as s:
        return [o.status for o in s.scalars(select(FileOp).order_by(FileOp.id))]


def test_write_new(ops: FileOps, sessions: sessionmaker[Session]) -> None:
    ops.write_new(REL, b"img", "b1")
    assert (ops.library / REL).read_bytes() == b"img" and ops.has(REL)
    assert statuses(sessions) == ["done"] and list(ops.incoming.iterdir()) == []


def test_write_new_never_replaces(ops: FileOps, sessions: sessionmaker[Session]) -> None:
    (ops.library / "A/B (2000)").mkdir(parents=True)
    (ops.library / REL).write_bytes(b"mine")
    with pytest.raises(FileExistsError):
        ops.write_new(REL, b"new", "b1")
    assert (ops.library / REL).read_bytes() == b"mine"
    assert statuses(sessions) == ["failed"]


def test_recover_completes_a_published_write(
    ops: FileOps, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(step: str) -> None:
        if step == "rename":
            raise KeyboardInterrupt

    monkeypatch.setattr(fileops, "_hook", crash)
    with pytest.raises(KeyboardInterrupt):
        ops.write_new(REL, b"img", "b1")
    assert statuses(sessions) == ["started"]
    ops.recover()
    assert statuses(sessions) == ["done"] and (ops.library / REL).read_bytes() == b"img"


def test_recover_rolls_back_before_publish(
    ops: FileOps, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(step: str) -> None:
        if step == "prerename":
            raise KeyboardInterrupt

    monkeypatch.setattr(fileops, "_hook", crash)
    with pytest.raises(KeyboardInterrupt):
        ops.write_new(REL, b"img", "b1")
    assert not Path(ops.library / REL).exists()
    assert statuses(sessions) == ["failed"]  # BaseException path already failed the op
