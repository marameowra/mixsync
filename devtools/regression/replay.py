"""Score the matcher regression corpus (#298) and compare it with the committed baseline.

    uv run pytest tests/regression                        # fails on any band or release change
    uv run python -m devtools.regression.replay --update  # accept the new results

Each case under `tests/regression/cases/` holds the matcher's inputs for one logged decision:
the peer's folder (stage 1), the downloaded files with their AcoustID results and tags
(stage 2), the user's profile, and the MusicBrainz release(s). Nothing here touches the network.
"""

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mixsync.acquire.requests import candidate_from_dict
from mixsync.core.matching import (
    AlbumInfo,
    FileTrack,
    Likelies,
    MatchResult,
    Profile,
    TrackInfo,
)
from mixsync.core.types import AcoustIdResult
from mixsync.match.candidates import score_candidate
from mixsync.match.scorer import best_release, score_files

ROOT = Path(__file__).resolve().parents[2] / "tests" / "regression"
CASES = ROOT / "cases"
BASELINE = ROOT / "baseline.json"


@dataclass(frozen=True)
class Outcome:
    band: str
    release: str | None
    result: MatchResult


def album(d: dict[str, Any]) -> AlbumInfo:
    tracks = tuple(replace(TrackInfo(**t), artist_ids=tuple(t["artist_ids"])) for t in d["tracks"])
    return replace(AlbumInfo(**d), tracks=tracks)


def file_track(d: dict[str, Any]) -> FileTrack:
    found = tuple(
        AcoustIdResult(a["id"], a["score"], tuple(a["recording_ids"])) for a in d["acoustid"]
    )
    return replace(FileTrack(**d), acoustid=found)


def load() -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text()) for p in sorted(CASES.glob("*.json"))}


def score(case: dict[str, Any]) -> dict[str, Outcome]:
    """Stage 1 against the decision's release; stage 2 picks among it and any repick."""
    profile = Profile(**case["profile"])
    albums = [album(a) for a in case["albums"]]
    s1 = score_candidate(candidate_from_dict(case["candidate"]), albums[0], profile)
    files = [file_track(f) for f in case["files"]]
    like = Likelies(**case["likelies"])
    if len(albums) == 1:
        picked, s2 = albums[0], score_files(files, like, albums[0], profile)
    else:
        picked, s2 = best_release(files, like, albums, profile)
    return {
        "stage1": Outcome(s1.band.value, None, s1),
        "stage2": Outcome(s2.band.value, picked.release_id, s2),
    }


def run() -> dict[str, dict[str, Any]]:
    """case id -> stage -> {band, release}: the shape of the baseline file."""
    return {
        name: {stage: {"band": o.band, "release": o.release} for stage, o in score(case).items()}
        for name, case in load().items()
    }


def explain(name: str, stage: str, case: dict[str, Any]) -> str:
    """Why a case scored as it did now: distance, biggest contributions, vetoes."""
    o = score(case)[stage]
    top = o.result.breakdown[:3]  # already largest share first
    lines = [
        f"  {name} {stage} [{', '.join(case['labels'])}] distance {o.result.distance:.3f}",
        *(
            f"    {p.key} penalty {p.penalty:.2f} x weight {p.weight:g}: {p.reason}"
            for p in top
            if p.penalty
        ),
        *(f"    veto: {v}" for v in o.result.vetoes),
    ]
    return "\n".join(lines)


def diff(baseline: dict[str, Any], now: dict[str, Any]) -> list[str]:
    """One readable block per case whose band or release moved, or that was added or removed."""
    cases = load()
    out: list[str] = []
    for name in sorted(baseline.keys() | now.keys()):
        if name not in now:
            out.append(f"- {name}: in the baseline but no case file")
            continue
        if name not in baseline:
            out.append(f"+ {name}: new case, not in the baseline")
            continue
        for stage, was in baseline[name].items():
            got = now[name][stage]
            if got == was:
                continue
            moved = [f"band {was['band']} -> {got['band']}"] if got["band"] != was["band"] else []
            if got["release"] != was["release"]:
                moved.append(f"release {was['release']} -> {got['release']}")
            out.append(f"~ {name} {stage}: {'; '.join(moved)}\n{explain(name, stage, cases[name])}")
    return out


def main(argv: list[str]) -> int:
    now = run()
    if "--update" in argv:
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        print(f"wrote {BASELINE} ({len(now)} cases)")
        return 0
    changes = diff(json.loads(BASELINE.read_text()), now)
    print("\n".join(changes) or f"no changes across {len(now)} cases")
    return 1 if changes else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
