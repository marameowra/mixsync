"""Fit the matcher's feature weights to human review outcomes (#299). Offline and read-only:

    uv run python -m devtools.calibrate <sqlite path or URL> [--min-samples 300] [--force]

It reads a database copy, never writes to it, and only prints proposed weights. Landing them is a
normal PR editing `DEFAULT_WEIGHTS`, gated by the regression suite (docs/design/matching.md).
"""

import argparse
import json
import math
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from sqlalchemy import Engine, create_engine, text

from devtools.regression.export import label_of
from mixsync.match.profiles import BALANCED
from mixsync.match.scorer import DEFAULT_WEIGHTS

MIN_SAMPLES = 300
THRESHOLD = BALANCED.review_max  # a distance above this counts as "reject"
SLOPE = 10.0  # logistic steepness around the threshold
RIDGE = 0.0002  # pull toward the current weights, so a thin sample cannot swing them wildly
STEPS = 3000
RATE = 5.0

Row = tuple[dict[str, float], bool]  # per-feature share of the distance, human accepted


def open_readonly(target: str) -> Engine:
    """SQLite via a `mode=ro` URI; Postgres with every transaction read-only."""
    if "://" in target and not target.startswith("sqlite"):
        ro = {"options": "-c default_transaction_read_only=on"}
        return create_engine(target, connect_args=ro)
    # a bare path is used as is; a sqlite:/// URL loses its scheme and query
    path = target.split("://", 1)[1].removeprefix("/").split("?")[0] if "://" in target else target
    path = path.removeprefix("file:")
    if path in ("", ":memory:"):
        raise SystemExit("devtools.calibrate needs a database file, not an in-memory database")
    file = Path(path).resolve()
    if not file.is_file():
        raise SystemExit(f"no database file at {file}")
    # as_uri percent-encodes '#', '?' and '%', which SQLite would otherwise read as URI syntax
    # and open (or create) a different file read-write
    uri = file.as_uri() + "?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


def load_rows(engine: Engine) -> list[Row]:
    """Decisions a human accepted or rejected, as per-feature distance shares. Each share is
    penalty * weight / max distance, so the current weights give back the stored distance."""
    query = (
        "select band, distance, breakdown, vetoes, evidence, status, final_action"
        " from match_decisions where final_action in ('accept', 'reject')"
    )
    rows: list[Row] = []
    with engine.connect() as conn:
        for r in conn.execute(text(query)).mappings():
            row = dict(r)
            # SQLite hands JSON columns back as text; Postgres JSON as parsed values
            for col in ("breakdown", "vetoes", "evidence"):
                if isinstance(row[col], str):
                    row[col] = json.loads(row[col])
            # label_of and the export read these as JSON text
            raw = {p["key"]: p["penalty"] * p["weight"] for p in row["breakdown"]}
            if row["distance"] <= 0 or not raw:
                continue  # no penalties logged: nothing to attribute
            as_text = {c: json.dumps(row[c]) for c in ("vetoes", "evidence")}
            labels = label_of({**row, **as_text})
            scale = sum(raw.values()) / row["distance"]  # max distance under the logged weights
            share = {
                k: v / DEFAULT_WEIGHTS[k] / scale for k, v in raw.items() if k in DEFAULT_WEIGHTS
            }
            rows.append((share, "accept" in labels))
    return rows


def distance(weights: Mapping[str, float], share: Mapping[str, float]) -> float:
    return sum(weights[k] * v for k, v in share.items())


def agreement(weights: Mapping[str, float], rows: Sequence[Row]) -> float:
    return sum((distance(weights, s) <= THRESHOLD) == ok for s, ok in rows) / len(rows)


def fit(rows: Sequence[Row]) -> dict[str, float]:
    """Logistic regression for "rejected" on the shares, weights kept non-negative, by gradient
    descent from the current weights. The result is rescaled so the accept/reject boundary stays
    at THRESHOLD, which keeps the weights in the units the bands use. Unseen ones keep theirs."""
    keys = sorted({k for s, _ in rows for k in s})
    w = {k: DEFAULT_WEIGHTS[k] * SLOPE for k in keys}
    bias = THRESHOLD * SLOPE
    start = dict(w)
    for _ in range(STEPS):
        grad = dict.fromkeys(keys, 0.0)
        grad_bias = 0.0
        for s, ok in rows:
            z = max(-30.0, min(30.0, distance(w, s) - bias))
            err = 1 / (1 + math.exp(-z)) - (not ok)
            for k, v in s.items():
                grad[k] += err * v
            grad_bias -= err
        for k in keys:
            w[k] = max(0.0, w[k] - RATE * (grad[k] / len(rows) + RIDGE * (w[k] - start[k])))
        bias -= RATE * grad_bias / len(rows)
    scale = THRESHOLD / bias if bias > 0 else 1.0
    return {**DEFAULT_WEIGHTS, **{k: round(v * scale, 3) for k, v in w.items()}}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="devtools.calibrate", description=__doc__)
    ap.add_argument("database", help="SQLite file or SQLAlchemy URL of a database copy")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    ap.add_argument("--force", action="store_true", help="fit even below --min-samples")
    args = ap.parse_args(argv)
    rows = load_rows(open_readonly(args.database))
    if len(rows) < args.min_samples:
        print(
            f"Only {len(rows)} usable accept/reject decisions; {args.min_samples} are needed for "
            "a stable fit." + ("" if args.force else " Pass --force to fit anyway."),
            file=sys.stderr,
        )
        if not args.force:
            return 1
    if not rows:
        return 1
    if len({ok for _, ok in rows}) < 2:
        outcome = "accepted" if rows[0][1] else "rejected"
        print(
            f"All {len(rows)} decisions were {outcome}; a fit needs both outcomes.",
            file=sys.stderr,
        )
        return 1
    proposed = fit(rows)
    print(f"samples: {len(rows)} ({sum(ok for _, ok in rows)} accepted)")
    print(f"{'feature':<16}{'current':>9}{'proposed':>10}")
    for k, cur in DEFAULT_WEIGHTS.items():
        print(f"{k:<16}{cur:>9.3f}{proposed[k]:>10.3f}{'  *' if proposed[k] != cur else ''}")
    # approximate: each decision keeps its logged max distance, since the breakdown omits
    # zero-penalty features; the scorer renormalises by the new weights
    print(f"approximate agreement with human outcomes, threshold {THRESHOLD}:")
    print(f"  before: {agreement(DEFAULT_WEIGHTS, rows):.1%}")
    print(f"  after:  {agreement(proposed, rows):.1%}")
    print("Proposals only (in-sample). Land them through a PR that passes the regression suite.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
