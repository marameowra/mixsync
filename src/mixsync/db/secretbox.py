"""Fernet encryption for stored user secrets. Key source: see db/README.md ("Secrets")."""

import logging
import os
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.core.config import Settings
from mixsync.db.models.accounts import UserLink

log = logging.getLogger(__name__)
_KEY_FILE = "secret.key"


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)  # ValueError on a malformed key, without echoing it

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        """None if this key can't decrypt the token (wrong or lost key)."""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None


def _box(key: bytes, what: str) -> SecretBox:
    try:
        return SecretBox(key)
    except ValueError:
        raise RuntimeError(f"{what} is not a valid Fernet key") from None  # never echo the key


def _create_key(path: Path) -> SecretBox:
    """Write a new key atomically: the file appears complete (hard link of a fsynced temp file)
    or not at all, and when two processes race the loser uses the winner's key."""
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    # the temp dir (mode 700, same filesystem) cleans itself up; delete calls stay in fileops.py
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".secret-key-") as d:
        tmp = Path(d) / _KEY_FILE
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _box(path.read_bytes().strip(), str(path))
    log.warning("Generated a new secret key at %s: back up this file", path)
    return _box(key, str(path))


def load_box(settings: Settings) -> SecretBox:
    """Env var wins; else the key file (refused if group/other can read it); else generate one."""
    path = Path(settings.config_dir) / _KEY_FILE
    if settings.secret_key:
        key = settings.secret_key.strip().encode()
        if path.exists() and path.read_bytes().strip() != key:
            log.warning("MIXSYNC_SECRET_KEY is set and differs from %s; using the env var", path)
        return _box(key, "MIXSYNC_SECRET_KEY")
    if not path.exists():
        return _create_key(path)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"{path} is readable by others; run: chmod 600 {path}")
    return _box(path.read_bytes().strip(), str(path))


def set_secret(box: SecretBox, link: UserLink, plain: str) -> None:
    link.secret_encrypted = box.encrypt(plain)
    link.needs_relink = False


def link_secret(box: SecretBox, link: UserLink) -> str | None:
    """The plaintext secret, or None (and `needs_relink` set) if the key can't decrypt it."""
    plain = box.decrypt(link.secret_encrypted)
    link.needs_relink = plain is None
    return plain


def user_links(s: Session, box: SecretBox, user_id: int) -> list[UserLink]:
    """A user's links with `needs_relink` brought up to date against the current key."""
    links = list(s.scalars(select(UserLink).where(UserLink.user_id == user_id)))
    for link in links:
        link_secret(box, link)
    return links
