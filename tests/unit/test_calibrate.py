import random
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from devtools.calibrate.__main__ import agreement, fit, load_rows, main, open_readonly
from mixsync.db.base import Base
from mixsync.db.models.matching import MatchDecision
from mixsync.match.scorer import DEFAULT_WEIGHTS

KEYS = ("artist", "album", "track_title")


def seed(path: Path, n: int) -> None:
    """Decisions whose outcome depends only on the title penalty; the other two are noise."""
    # sqlite3 takes the path as is; a sqlite:/// URL would decode '%' and drop a '?' suffix
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(path))
    Base.metadata.create_all(engine)
    rng = random.Random(1)
    with engine.begin() as conn:
        for _ in range(n):
            pen = {k: rng.random() for k in KEYS}
            pen["track_title"] = rng.choice([0.05, 0.9])
            total = sum(pen[k] * DEFAULT_WEIGHTS[k] for k in KEYS) / sum(
                DEFAULT_WEIGHTS[k] for k in KEYS
            )
            conn.execute(
                MatchDecision.__table__.insert().values(
                    user_id=1,
                    stage=2,
                    release_mbid="m",
                    band="review",
                    distance=total,
                    breakdown=[
                        {"key": k, "penalty": pen[k], "weight": DEFAULT_WEIGHTS[k], "reason": ""}
                        for k in KEYS
                    ],
                    vetoes=[],
                    scorer_version=1,
                    evidence={},
                    status="done",
                    final_action="reject" if pen["track_title"] > 0.5 else "accept",
                )
            )


def test_weight_of_deciding_feature_rises_and_agreement_improves(tmp_path: Path) -> None:
    seed(tmp_path / "db.sqlite", 120)
    rows = load_rows(open_readonly(str(tmp_path / "db.sqlite")))
    proposed = fit(rows)
    assert proposed["track_title"] > DEFAULT_WEIGHTS["track_title"]
    assert agreement(proposed, rows) > agreement(DEFAULT_WEIGHTS, rows)
    assert proposed["media"] == DEFAULT_WEIGHTS["media"]  # unseen features keep their weight


def test_connection_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    seed(db, 5)
    engine = open_readonly(str(db))
    with pytest.raises(OperationalError), engine.begin() as conn:
        conn.execute(text("update match_decisions set final_action = 'accept'"))
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(MatchDecision.__table__)) == 5


@pytest.mark.parametrize("name", ["copy#1.db", "pc%41.db", "a?b.db", "with space.db"])
def test_uri_characters_in_the_path_still_open_read_only(tmp_path: Path, name: str) -> None:
    db = tmp_path / name
    seed(db, 5)
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(OperationalError), open_readonly(str(db)).begin() as conn:
        conn.execute(text("create table t (x int)"))
    with open_readonly(str(db)).connect() as conn:
        assert conn.scalar(select(func.count()).select_from(MatchDecision.__table__)) == 5
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # no stray file created


@pytest.mark.parametrize("target", ["sqlite://", "sqlite:///:memory:", "missing.db"])
def test_no_database_file_exits_with_a_message(tmp_path: Path, target: str) -> None:
    with pytest.raises(SystemExit, match="database file"):
        open_readonly(target if "://" in target else str(tmp_path / target))


def test_one_outcome_only_refuses_to_fit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "db.sqlite"
    seed(db, 10)
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(text("update match_decisions set final_action = 'reject'"))
    assert main([str(db), "--force"]) == 1
    assert "All 10 decisions were rejected" in capsys.readouterr().err


def test_below_threshold_exits_nonzero_unless_forced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "db.sqlite"
    seed(db, 40)
    assert main([str(db)]) == 1
    assert "40 usable" in capsys.readouterr().err
    assert main([str(db), "--force"]) == 0
    out = capsys.readouterr().out
    assert "before:" in out and "after:" in out and "track_title" in out
    assert main([str(db), "--min-samples", "40"]) == 0
