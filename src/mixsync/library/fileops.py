"""The only module that writes under the data dir. See docs/design/data-safety.md.

Nothing here deletes: leftovers and trashed files are moved into `.trash/YYYY-MM-DD/`.
Journal rows: `src` and `dst` of a copy op are the source path and the final library-relative
path; the staging file is always `.incoming/<op_id>`. `dst_hash` is journaled before the
rename, so `recover()` can tell a finished import from an abandoned one.
"""

import ctypes
import errno
import hashlib
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from mixsync.core.clock import Clock
from mixsync.core.errors import SafetyError
from mixsync.core.protocols import Journal, JournalOp
from mixsync.library.paths import unique

log = logging.getLogger(__name__)

WriteTags = Callable[[Path, int], None]  # (staging file, op_id)


def _no_hook(_step: str) -> None:
    return None


_hook: Callable[[str], None] = _no_hook  # crash tests replace this
_libc = ctypes.CDLL(None, use_errno=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _fsync(path: Path, flags: int = os.O_RDONLY) -> None:
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _noreplace(src: Path, dst: Path) -> bool:
    """rename(2) that fails with FileExistsError instead of replacing dst, as one atomic
    syscall (no check-then-rename race): renameatx_np(RENAME_EXCL) on macOS, renameat2(
    RENAME_NOREPLACE) on Linux. False if this OS or filesystem lacks it (EINVAL/ENOTSUP/
    ENOSYS, e.g. some network filesystems); the caller then falls back."""
    if sys.platform == "darwin":
        fn, at, flag = getattr(_libc, "renameatx_np", None), -2, 0x4  # AT_FDCWD, RENAME_EXCL
    else:
        fn, at, flag = getattr(_libc, "renameat2", None), -100, 1  # AT_FDCWD, RENAME_NOREPLACE
    if fn is None:
        return False
    if fn(at, os.fsencode(src), at, os.fsencode(dst), flag) == 0:
        return True
    err = ctypes.get_errno()
    if err in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
        return False
    raise OSError(err, os.strerror(err), str(dst))


class FileOps:
    def __init__(self, data_dir: Path, journal: Journal, clock: Clock) -> None:
        self.library = data_dir / "library"
        self.incoming = data_dir / ".incoming"
        self.trash_dir = data_dir / ".trash"
        self._journal = journal
        self._clock = clock

    def _under(self, root: Path, rel: str) -> Path:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise SafetyError(f"path escapes {root}: {rel!r}")
        return root / p

    def _trash_rel(self, rel: str) -> str:
        day = self._clock.now().date().isoformat()
        return unique(f"{day}/{rel}", lambda r: (self.trash_dir / r).exists())

    def _move_to_trash(self, src: Path, trash_rel: str) -> None:
        """Only fileops writes to the trash and trash_rel is a free name, so the plain-rename
        fallback (no no-replace rename on this filesystem) cannot overwrite anything."""
        dst = self.trash_dir / trash_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not _noreplace(src, dst):
                os.rename(src, dst)
        except OSError as e:
            if e.errno == errno.EXDEV:
                raise SafetyError(f"{src} and {dst} are on different devices") from e
            raise

    def _to_trash(self, path: Path, rel: str) -> None:
        self._move_to_trash(path, self._trash_rel(rel))

    def _publish(self, staging: Path, final: Path, op_id: int) -> None:
        try:
            if not _noreplace(staging, final):
                # link(2) is also atomic and fails with EEXIST. Both names then point to one
                # inode, so the staging name goes to the trash rather than being unlinked.
                os.link(staging, final)
                self._to_trash(staging, f".incoming/{op_id}")
        except OSError as e:
            if e.errno == errno.EXDEV:
                raise SafetyError(f"{staging} and {final} are on different devices") from e
            raise

    def import_file(self, src: Path, final_rel: str, write_tags: WriteTags, batch_id: str) -> str:
        """Copy src into the library at final_rel (verified, tagged, atomic, never replacing
        a file). Returns the sha256 of the imported file. src is never modified."""
        final = self._under(self.library, final_rel)
        op_id = self._journal.begin(batch_id, "copy", str(src), final_rel)
        staging = self.incoming / str(op_id)
        published = False
        try:
            self.incoming.mkdir(parents=True, exist_ok=True)
            with src.open("rb") as r, staging.open("xb") as w:
                while chunk := r.read(1 << 20):
                    w.write(chunk)
            _hook("copy")
            _fsync(staging)
            _fsync(self.incoming)
            if _sha256(staging) != _sha256(src):
                raise SafetyError(f"hash mismatch copying {src}")
            _hook("fsync")
            write_tags(staging, op_id)
            _fsync(staging)
            _hook("tag")
            digest = _sha256(staging)
            self._journal.set_dst_hash(op_id, digest)
            final.parent.mkdir(parents=True, exist_ok=True)
            _hook("prerename")
            self._publish(staging, final, op_id)
            published = True
            _hook("rename")
            _fsync(final.parent)
        except BaseException as e:
            if published:
                raise  # in the library: stay pending so recover() verifies and completes
            if staging.exists():
                self._to_trash(staging, f".incoming/{op_id}")
            self._journal.fail(op_id, repr(e))
            raise
        self._journal.complete(op_id, digest)
        return digest

    def has(self, rel: str) -> bool:
        return self._under(self.library, rel).exists()

    def write_new(self, rel: str, data: bytes, batch_id: str) -> None:
        """Create a library file from bytes (cover art). Same journal/staging/no-replace publish
        as import_file, so recover() treats it as a copy. Raises FileExistsError, leaving the
        existing file alone, if rel is taken."""
        final = self._under(self.library, rel)
        op_id = self._journal.begin(batch_id, "copy", "(generated)", rel)
        staging = self.incoming / str(op_id)
        published = False
        try:
            self.incoming.mkdir(parents=True, exist_ok=True)
            with staging.open("xb") as w:
                w.write(data)
            _fsync(staging)
            _fsync(self.incoming)
            digest = _sha256(staging)
            if digest != hashlib.sha256(data).hexdigest():
                raise SafetyError(f"hash mismatch writing {rel}")
            self._journal.set_dst_hash(op_id, digest)
            final.parent.mkdir(parents=True, exist_ok=True)
            _hook("prerename")
            self._publish(staging, final, op_id)
            published = True
            _hook("rename")
            _fsync(final.parent)
        except BaseException as e:
            if published:
                raise
            if staging.exists():
                self._to_trash(staging, f".incoming/{op_id}")
            self._journal.fail(op_id, repr(e))
            raise
        self._journal.complete(op_id, digest)

    def find_imported(self, batch_id: str, src: Path) -> str | None:
        """Library path of an earlier completed import of src in this batch, if the file is
        still there and intact."""
        op = self._journal.find_completed(batch_id, str(src))
        if op and op.dst_hash and (f := self.library / op.dst).is_file():
            return op.dst if _sha256(f) == op.dst_hash else None
        return None

    def trash(self, rel: str, batch_id: str) -> str:
        """Move a library file to `.trash/<day>/<rel>`. Returns the trash-relative path."""
        src = self._under(self.library, rel)
        trash_rel = self._trash_rel(rel)
        op_id = self._journal.begin(batch_id, "trash", rel, trash_rel)
        try:
            self._move_to_trash(src, trash_rel)
        except BaseException as e:
            self._journal.fail(op_id, repr(e))
            raise
        self._journal.complete(op_id, _sha256(self.trash_dir / trash_rel))
        return trash_rel

    def recover(self) -> None:
        """Finish or roll back every op that was started and never finished."""
        for op in self._journal.pending():
            if op.kind == "copy":
                self._recover_copy(op)
            elif op.kind == "trash":
                self._recover_trash(op)

    def _recover_copy(self, op: JournalOp) -> None:
        final = self.library / op.dst
        staging = self.incoming / str(op.id)
        if staging.exists():
            self._to_trash(staging, f".incoming/{op.id}")
        if op.dst_hash and final.is_file() and _sha256(final) == op.dst_hash:
            self._journal.complete(op.id, op.dst_hash)
            log.warning("recovered copy op %s: completed", op.id)
        else:
            self._journal.rolled_back(op.id)
            log.warning("recovered copy op %s: rolled back", op.id)

    def _recover_trash(self, op: JournalOp) -> None:
        src, dst = self.library / op.src, self.trash_dir / op.dst
        if dst.is_file() and not src.exists():
            self._journal.complete(op.id, _sha256(dst))
        else:
            self._journal.rolled_back(op.id)
