import random
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
    engine = create_engine(f"sqlite:///{path}")
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
