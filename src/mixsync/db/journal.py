from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock
from mixsync.core.protocols import JournalOp
from mixsync.db.models.safety import FileOp


class SqlJournal:
    """Journal over file_ops. Every call commits before returning."""

    def __init__(self, sessions: sessionmaker[Session], clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock

    def begin(self, batch_id: str, kind: str, src: str, dst: str) -> int:
        with self._sessions.begin() as s:
            op = FileOp(
                batch_id=batch_id,
                kind=kind,
                src=src,
                dst=dst,
                status="started",
                started_at=self._clock.now(),
            )
            s.add(op)
            s.flush()
            return op.id

    def complete(self, op_id: int, dst_hash: str) -> None:
        self._finish(op_id, "done", dst_hash=dst_hash)

    def set_dst_hash(self, op_id: int, dst_hash: str) -> None:
        with self._sessions.begin() as s:
            s.get_one(FileOp, op_id).dst_hash = dst_hash

    def fail(self, op_id: int, error: str) -> None:
        self._finish(op_id, "failed", error=error)

    def rolled_back(self, op_id: int) -> None:
        self._finish(op_id, "rolled_back")

    def pending(self) -> list[JournalOp]:
        with self._sessions() as s:
            rows = s.scalars(select(FileOp).where(FileOp.status == "started").order_by(FileOp.id))
            return [_to_op(r) for r in rows]

    def _finish(
        self, op_id: int, status: str, dst_hash: str | None = None, error: str | None = None
    ) -> None:
        with self._sessions.begin() as s:
            op = s.get_one(FileOp, op_id)
            op.status = status
            op.finished_at = self._clock.now()
            if dst_hash is not None:
                op.dst_hash = dst_hash
            if error is not None:
                op.error = error


def _to_op(r: FileOp) -> JournalOp:
    return JournalOp(
        id=r.id,
        batch_id=r.batch_id,
        kind=r.kind,
        src=r.src,
        dst=r.dst,
        src_hash=r.src_hash,
        dst_hash=r.dst_hash,
        status=r.status,
        started_at=r.started_at,
        finished_at=r.finished_at,
        error=r.error,
    )
