"""Restore a library file's earlier tags: trash it, then copy it back from the trash with the old
tags written to the copy (journaled, verified, the current tags snapshotted first).

The path is empty between the trash and the publish. `unfinished` finds a restore that died
there; the worker puts the original back at startup (`recover`) and the purge keeps its trash
until then."""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock
from mixsync.db.models.safety import FileOp
from mixsync.library import tags
from mixsync.library.fileops import FileOps
from mixsync.library.importer import snapshotting

log = logging.getLogger(__name__)
BATCH = "tag-undo-"


def restore_tags(
    fo: FileOps,
    sessions: sessionmaker[Session],
    clock: Clock,
    track_id: int,
    rel: str,
    old: tags.Snapshot,
) -> None:
    batch = f"{BATCH}{uuid.uuid4().hex}"
    trashed = fo.trash(rel, batch)
    change = snapshotting(
        sessions, clock, rel, lambda staging: tags.restore(staging, old), track_id
    )
    try:
        fo.import_file(fo.trash_dir / trashed, rel, change, batch)
    except BaseException:
        if not fo.has(rel):  # failed before publishing: put the original back
            try:
                fo.restore(trashed, rel, batch)
            except Exception:  # e.g. the database is locked: recover() retries at startup
                log.exception("tag undo: could not put %s back; the worker will", rel)
        raise


def unfinished(sessions: sessionmaker[Session], fo: FileOps) -> list[FileOp]:
    """Trash ops of tag restores that never published and whose library path is empty."""
    with sessions() as s:
        trashed = s.scalars(
            select(FileOp).where(
                FileOp.kind == "trash",
                FileOp.status.in_(("done", "started")),  # the web process may die before complete
                FileOp.batch_id.like(f"{BATCH}%"),
            )
        )
        done = set(
            s.scalars(
                select(FileOp.batch_id).where(
                    FileOp.kind == "copy",
                    FileOp.status == "done",
                    FileOp.batch_id.like(f"{BATCH}%"),
                )
            )
        )
        return [op for op in trashed if op.batch_id not in done and not fo.has(op.src)]


def recover(sessions: sessionmaker[Session], fo: FileOps) -> None:
    """After `FileOps.recover()`: put back each original a crashed tag restore left in the trash."""
    for op in unfinished(sessions, fo):
        try:
            fo.restore(op.dst, op.src, op.batch_id)
            log.warning("tag undo: put %s back", op.src)
        except FileExistsError:  # a request published it meanwhile
            pass
