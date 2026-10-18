"""Adopt album folders nobody requested that land in downloads (a hand-made slskd download).

The drop folder over `downloads_dir`: same scan, settle check and pipeline, but a verified import
releases the originals and a rejected one goes to review. What MixSync itself downloaded or
verified is left alone, whatever became of it: rejected and failed downloads stay in downloads.
"""

from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.dropfolder import DropFolder
from mixsync.core.clock import Clock
from mixsync.core.jobs import JobKind
from mixsync.core.matching import download_dir
from mixsync.db.models.safety import FileOp
from mixsync.db.models.work import Job, Request


def owned_dirs(s: Session, downloads: Path) -> set[Path]:
    """Directories MixSync downloaded into or verified from, by any job's status. Directory
    level on purpose: a false skip is safe, a second import is not."""
    # shortcut: reads every job payload per scan; index or prune if the jobs table grows huge
    out: set[Path] = set()
    rows = s.execute(
        select(Job.payload, Job.idempotency_key).where(
            Job.kind.in_([JobKind.DOWNLOAD, JobKind.VERIFY]),
            # the monitor rows repeat the download's own candidate
            or_(Job.idempotency_key.is_(None), ~Job.idempotency_key.like("monitor:%")),
        )
    )
    for payload, key in rows:
        if key and key.startswith("adopt:"):  # the adopter's own: its digest key dedupes
            continue
        out |= {Path(p).parent for p in payload.get("paths", [])}
        files = payload.get("candidate", {}).get("files", [])
        out |= {download_dir(downloads, f["path"]) for f in files}
    out |= {Path(src).parent for src in s.scalars(select(FileOp.src).where(FileOp.kind == "copy"))}
    for tried in s.scalars(select(Request.tried)):
        out |= {download_dir(downloads, f"{folder}\\") for _, folder in tried}
    return out


def adopter(
    sessions: sessionmaker[Session],
    clock: Clock,
    downloads: Path,
    work_dir: Path,
    enabled: bool,
    settle_minutes: int,
) -> DropFolder:
    # shortcut: no check for unfinished slskd transfers; a peer that queues the rest of an album
    # for longer than the settle window gets it adopted half-done. Ask slskd if that bites.
    return DropFolder(
        sessions,
        clock,
        downloads if enabled else None,
        work_dir,
        kind=JobKind.ADOPT_SCAN,
        source="adopt",
        settle=timedelta(minutes=settle_minutes),
        owned=lambda s: owned_dirs(s, downloads),
    )
