"""The only module that writes under the data dir. See docs/design/data-safety.md.

The only deletes are `release()`, which unlinks a hash-verified download from the downloads dir,
and `clear_work()`, which empties MixSync's own re-encode scratch space; leftovers and trashed
files are moved into `.trash/YYYY-MM-DD/` (`.trash/YYYY-MM-DD/.discover/` for the discover
root). Two roots, `library` and `discover`, live under the data dir; every journal row records
its `root` and `recover()` resolves relative paths against it. Discover must be on the same
filesystem as `.incoming` and `.trash`, or every discover import fails safely with EXDEV.
Journal rows: `src` and `dst` of a copy op are the source path and the final library-relative
path; the staging file is always `.incoming/<op_id>`. `dst_hash` is journaled before the
rename, so `recover()` can tell a finished import from an abandoned one.
"""

import ctypes
import errno
import hashlib
import logging
import os
import shutil
import sys
from collections.abc import Callable
from contextlib import suppress
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
    def __init__(self, data_dir: Path, downloads_dir: Path, journal: Journal, clock: Clock) -> None:
        self.library = data_dir / "library"
        self.roots = {"library": self.library, "discover": data_dir / "discover"}
        self.incoming = data_dir / ".incoming"
        self.trash_dir = data_dir / ".trash"
        self.work = data_dir / ".work"  # scratch space for re-encoded copies of downloads
        self._downloads = downloads_dir
        self._journal = journal
        self._clock = clock

    def _root(self, root: str) -> Path:
        if root not in self.roots:
            raise SafetyError(f"unknown root: {root!r}")
        return self.roots[root]

    def _under(self, root: Path, rel: str) -> Path:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise SafetyError(f"path escapes {root}: {rel!r}")
        return root / p

    def _trash_rel(self, rel: str, root: str = "library") -> str:
        day = self._clock.now().date().isoformat()
        if root == "library" and rel.split("/")[0] == ".discover":
            raise SafetyError(f"{rel!r} would collide with the discover trash prefix")
        # sanitize() strips leading dots, so no rendered library path starts with ".discover"
        sub = "" if root == "library" else f".{root}/"
        return unique(f"{day}/{sub}{rel}", lambda r: (self.trash_dir / r).exists())

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

    def import_file(
        self,
        src: Path,
        final_rel: str,
        write_tags: WriteTags,
        batch_id: str,
        origin: Path
        | None = None,  # the download src was made from; journaled so it can be released
        root: str = "library",
    ) -> str:
        """Copy src into the library at final_rel (verified, tagged, atomic, never replacing
        a file). Returns the sha256 of the imported file. src is never modified."""
        final = self._under(self._root(root), final_rel)
        op_id = self._journal.begin(batch_id, "copy", str(origin or src), final_rel, root=root)
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

    def has(self, rel: str, root: str = "library") -> bool:
        return self._under(self._root(root), rel).exists()

    def write_new(self, rel: str, data: bytes, batch_id: str, root: str = "library") -> None:
        """Create a library file from bytes (cover art). Same journal/staging/no-replace publish
        as import_file, so recover() treats it as a copy. Raises FileExistsError, leaving the
        existing file alone, if rel is taken."""
        final = self._under(self._root(root), rel)
        op_id = self._journal.begin(batch_id, "copy", "(generated)", rel, root=root)
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

    def find_imported(self, batch_id: str, src: Path, root: str = "library") -> str | None:
        """Path (relative to root) of an earlier completed import of src in this batch, if the
        file is still there and intact."""
        op = self._journal.find_completed(batch_id, str(src), root)
        if op and op.root == root and op.dst_hash and (f := self._root(root) / op.dst).is_file():
            return op.dst if _sha256(f) == op.dst_hash else None
        return None

    def release(self, src: Path, batch_id: str, root: str = "library") -> None:
        """Delete a download once its copy under root from this batch is verified. Refuses
        anything not strictly inside the downloads dir (symlinks and `..` included)."""
        dl = self._downloads.resolve()
        if ".." in src.parts or src.is_symlink() or dl not in src.resolve().parents:
            raise SafetyError(f"{src} is not inside {dl}")
        rel = self.find_imported(batch_id, src, root)
        op = self._journal.find_completed(batch_id, str(src), root)
        if rel is None or op is None or op.dst_hash is None:
            raise SafetyError(f"{src} has no verified import in batch {batch_id}")
        op_id = self._journal.begin(batch_id, "release", str(src), rel, root=root)
        self._journal.set_dst_hash(op_id, op.dst_hash)
        try:
            _hook("release")
            src.unlink(missing_ok=True)
            if src.parent.resolve() != dl:  # never the root itself
                with suppress(OSError):  # not empty
                    src.parent.rmdir()
        except Exception as e:  # an interrupt stays pending: recover() settles it
            self._journal.fail(op_id, repr(e))
            raise
        self._journal.complete(op_id, op.dst_hash)

    def clear_work(self, batch_id: str) -> None:
        """Delete the scratch files of one batch. Only ever acts inside the work dir."""
        d = self._under(self.work, batch_id)
        if d.is_dir() and not d.is_symlink():
            shutil.rmtree(d, ignore_errors=True)  # noqa: TID251

    def trash(self, rel: str, batch_id: str, root: str = "library") -> str:
        """Move a file to `.trash/<day>/<rel>` (`.trash/<day>/<root>/<rel>` for other roots).
        Returns the trash-relative path."""
        src = self._under(self._root(root), rel)
        trash_rel = self._trash_rel(rel, root)
        op_id = self._journal.begin(batch_id, "trash", rel, trash_rel, root=root)
        try:
            self._move_to_trash(src, trash_rel)
        except BaseException as e:
            self._journal.fail(op_id, repr(e))
            raise
        self._journal.complete(op_id, _sha256(self.trash_dir / trash_rel))
        return trash_rel

    def restore(self, trash_rel: str, rel: str, batch_id: str, root: str = "library") -> None:
        """Move a trashed file back to path rel under root. Never replaces a file: raises
        FileExistsError if rel is taken, leaving both alone."""
        src = self._under(self.trash_dir, trash_rel)
        dst = self._under(self._root(root), rel)
        parts = Path(trash_rel).parts
        trashed_from = parts[1][1:] if len(parts) > 1 and parts[1] == ".discover" else "library"
        if trashed_from != root:
            raise SafetyError(f"{trash_rel!r} was not trashed from root {root!r}")
        op_id = self._journal.begin(batch_id, "restore", trash_rel, rel, root=root)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            _hook("restore")
            if not _noreplace(src, dst):
                # link(2) is atomic and fails with EEXIST; the trash name stays (it is trash).
                os.link(src, dst)
            _fsync(dst.parent)
        except OSError as e:
            self._journal.fail(op_id, repr(e))
            if e.errno == errno.EXDEV:
                raise SafetyError(f"{src} and {dst} are on different devices") from e
            raise
        self._journal.complete(op_id, _sha256(dst))

    def recover(self) -> None:
        """Finish or roll back every op that was started and never finished."""
        if self.work.is_dir():  # scratch of a crashed run
            for d in self.work.iterdir():
                self.clear_work(d.name)
        for op in self._journal.pending():
            if op.kind == "copy":
                self._recover_copy(op)
            elif op.kind == "trash":
                self._recover_trash(op)
            elif op.kind == "release":
                self._recover_release(op)
            elif op.kind == "restore":
                self._recover_restore(op)

    def _recover_copy(self, op: JournalOp) -> None:
        final = self._root(op.root) / op.dst
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
        src, dst = self._root(op.root) / op.src, self.trash_dir / op.dst
        if dst.is_file() and not src.exists():
            self._journal.complete(op.id, _sha256(dst))
        else:
            self._journal.rolled_back(op.id)

    def _recover_release(self, op: JournalOp) -> None:
        if Path(op.src).exists():
            self._journal.rolled_back(op.id)  # the next import retry releases it again
        else:
            self._journal.complete(op.id, op.dst_hash or "")

    def _recover_restore(self, op: JournalOp) -> None:
        src, dst = self.trash_dir / op.src, self._root(op.root) / op.dst
        # The file is back if it is at dst and the trash name is gone (or is the same inode).
        if dst.is_file() and (not src.exists() or src.samefile(dst)):
            self._journal.complete(op.id, _sha256(dst))
        else:
            self._journal.rolled_back(op.id)
