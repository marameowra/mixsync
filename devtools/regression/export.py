"""Dump matcher inputs for logged decisions as JSON lines, for the regression corpus (#298).

Runs inside the mixsync container, read-only: the DB is opened with `mode=ro`, the audio is only
read, and nothing is fetched. Usage (from the repo root):

    ssh elitedesk 'sudo podman exec -i mixsync nice python - 100' \\
        < devtools/regression/export.py > "$TMP/inputs.jsonl"

then `uv run python -m devtools.regression.build_cases "$TMP/inputs.jsonl"`.

Stage-2 inputs are rebuilt with the pipeline's own `file_track`/`likelies`. Files mixsync has
since retagged are read through their pre-import tag snapshot, so the tags are the ones the
scorer saw. Peer names are hashed and paths cut to what the scorer reads.
"""

import asyncio
import hashlib
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import mutagen
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from mixsync.acquire import pipeline
from mixsync.acquire.requests import profile_for
from mixsync.core.types import AcoustIdResult
from mixsync.library import measure, tags

DB = "sqlite:///file:/config/mixsync.db?mode=ro&uri=true"

# (label, how to spot it, how many to take); earlier labels claim a decision first
LABELS: list[tuple[str, str, int]] = [
    ("acoustid_contradicts", "says a different recording", 12),
    ("tagged_mismatch", "tagged as recording", 8),
    ("transcode", "looks transcoded", 12),
    ("padded", "duration off by", 12),
    ("partial", "", 6),
    ("repick", "", 14),
    ("reject", "", 12),
    ("accept", "", 12),
    ("auto_accept", "", 12),
]


def label_of(row: dict[str, Any]) -> list[str]:
    vetoes = " ".join(json.loads(row["vetoes"]))
    ev = json.loads(row["evidence"])
    out = [name for name, needle, _ in LABELS if needle and needle in vetoes]
    if ev.get("partial"):
        out.append("partial")
    if row["status"] == "repick":
        out.append("repick")
    if row["final_action"] in ("reject", "accept"):
        out.append(row["final_action"])
    if row["band"] == "auto_accept" and row["final_action"] is None:
        out.append("auto_accept")
    return out


def peer(name: str) -> str:
    return "peer-" + hashlib.sha256(name.encode()).hexdigest()[:8] if name else ""


def tail(path: str, keep: int) -> str:
    """The last `keep` parts, backslash-joined as slskd reports them: the scorer reads only the
    name and its folder. Local imports use `/`."""
    return "\\".join(re.split(r"[\\/]", path)[-keep:])


def redact(cand: dict[str, Any]) -> dict[str, Any]:
    return {
        **cand,
        "username": peer(cand["username"]),
        "folder": tail(cand["folder"], 2),
        "files": [{**f, "path": tail(f["path"], 2)} for f in cand["files"]],
    }


def snapshots(s: Session, decision_id: int) -> dict[str, dict[str, Any]]:
    """Pre-import tags by library-relative path, for the files this decision imported."""
    rows = s.execute(
        text(
            "select t.file_path, t.tags from tag_snapshots t join file_ops o on o.id = t.op_id"
            " where o.batch_id like :b order by t.id"
        ),
        {"b": f"request-%-decision-{decision_id}"},
    )
    out: dict[str, dict[str, Any]] = {}
    for rel, snap in rows:
        out.setdefault(rel, json.loads(snap))  # the first snapshot predates any retag
    return out


async def stage2(s: Session, row: dict[str, Any]) -> dict[str, Any] | None:
    ev = json.loads(row["evidence"])
    listed = ev["source"]["files"]
    found = ev.get("acoustid", [])
    if len(found) != len(listed):  # an unreadable file shifted the alignment
        return None
    paths = [Path(f["local_path"] or "") for f in listed]
    if not all(p.is_file() for p in paths):
        return None  # purged from quarantine, or never kept
    snaps = snapshots(s, row["id"])
    read_all = tags.read_all

    def original(p: Path) -> Any:
        hit = next((v for k, v in snaps.items() if str(p).endswith("/" + k)), None)
        return hit or read_all(p)

    tags.read_all = original  # file_track and likelies read tags only through this
    try:
        files: list[dict[str, Any]] = []
        for p, a in zip(paths, found, strict=True):
            info = mutagen.File(p)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            # shortcut: mutagen's length, not fpcalc's; within a frame for intact files
            length = float(info.info.length) if info else 0.0  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            results = (AcoustIdResult("", a["score"], tuple(a["recording_ids"])),) if a else ()
            ft = pipeline.file_track(
                p,
                length,
                results,
                await measure.high_band_share(p),
                await measure.trailing_silence(p, length),
            )
            files.append(asdict(ft))
        like = asdict(pipeline.likelies(paths))
    finally:
        tags.read_all = read_all
    return {"files": files, "likelies": like}


async def main(target: int) -> None:
    engine = create_engine(DB)
    with Session(engine) as s:
        rows = [
            dict(r)
            for r in s.execute(
                text(
                    "select id, user_id, release_mbid, release_given, band, distance, vetoes,"
                    " scorer_version, status, final_action, chosen_release_mbid, evidence"
                    " from match_decisions where stage = 2 and release_mbid != ''"
                    " and vetoes not like '%unreadable%' order by id desc"
                )
            ).mappings()
        ]
        quota = {name: round(n * target / 100) for name, _, n in LABELS}
        done = 0
        for row in rows:
            labels = label_of(row)
            want = [x for x in labels if quota.get(x, 0) > 0]
            if not want:
                continue
            ev = json.loads(row["evidence"])
            if "candidate" not in ev:
                continue
            inputs = await stage2(s, row)
            if inputs is None:
                continue
            for x in want:
                quota[x] -= 1
            profile = profile_for(s, row["user_id"])
            out = {
                "decision": row["id"],
                "labels": labels,
                "profile": asdict(profile),
                "release_mbid": row["release_mbid"],
                "chosen_release_mbid": row["chosen_release_mbid"],
                "release_given": bool(row["release_given"]),
                "recorded": {k: row[k] for k in ("band", "distance", "scorer_version", "status")}
                | {"final_action": row["final_action"], "vetoes": json.loads(row["vetoes"])},
                "candidate": redact(ev["candidate"]),
                **inputs,
            }
            print(json.dumps(out), flush=True)
            done += 1
            print(f"{done} decision {row['id']} {labels}", file=sys.stderr, flush=True)
            if not any(v > 0 for v in quota.values()):
                break
        print(f"unfilled quotas: { {k: v for k, v in quota.items() if v > 0} }", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 100))
