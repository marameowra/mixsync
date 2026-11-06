"""Turn `export.py` output into corpus cases: fetch each release from MusicBrainz and write
`tests/regression/cases/d<decision>.json`. Existing case files are left alone.

    uv run python -m devtools.regression.build_cases inputs.jsonl

Fetches go through PoliteClient (1 req/s, MixSync User-Agent) with a throwaway cache DB.
"""

import asyncio
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import mixsync.db.models  # noqa: F401  # pyright: ignore[reportUnusedImport]  # registers the tables
from devtools.regression.replay import CASES
from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError
from mixsync.db.base import Base
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient


async def main(lines: list[str]) -> None:
    CASES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{tmp}/cache.db"
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        settings = Settings(database_url=url, config_dir=tmp)
        async with PoliteClient("musicbrainz", settings, sessionmaker(engine)) as http:
            mb = MusicBrainzProvider(http, settings.mb_base_url)
            for line in lines:
                case = json.loads(line)
                out = CASES / f"d{case['decision']}.json"
                if out.exists():
                    continue
                ids = [case["release_mbid"]]
                if (chosen := case["chosen_release_mbid"]) and chosen not in ids:
                    ids.append(chosen)
                try:
                    case["albums"] = [asdict(await mb.get_release(i)) for i in ids]
                except PermanentError as e:  # merged or deleted since: the case cannot replay
                    print(f"skip d{case['decision']}: {e}", file=sys.stderr)
                    continue
                out.write_text(json.dumps(case, indent=1, sort_keys=True) + "\n")
                print(f"wrote {out.name}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).read_text().splitlines()))
