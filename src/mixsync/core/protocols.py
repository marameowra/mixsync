from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class JournalOp:
    id: int
    batch_id: str
    kind: str  # copy / move / trash / retag / purge
    src: str
    dst: str
    src_hash: str | None
    dst_hash: str | None
    status: str  # started / done / failed / rolled_back
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class Journal(Protocol):
    def begin(self, batch_id: str, kind: str, src: str, dst: str) -> int: ...
    def complete(self, op_id: int, dst_hash: str) -> None: ...
    def fail(self, op_id: int, error: str) -> None: ...
    def pending(self) -> list[JournalOp]: ...
