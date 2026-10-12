"""Fernet encryption for stored user secrets. Key source: see db/README.md ("Secrets")."""

import logging
import stat
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


def load_box(settings: Settings) -> SecretBox:
    """Env var wins; else the key file (refused if group/other can read it); else generate one."""
    path = Path(settings.config_dir) / _KEY_FILE
    if settings.secret_key:
        key = settings.secret_key.strip().encode()
        if path.exists() and path.read_bytes().strip() != key:
            log.warning("MIXSYNC_SECRET_KEY is set and differs from %s; using the env var", path)
        return SecretBox(key)
    if path.exists():
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError(f"{path} is readable by others; run: chmod 600 {path}")
        return SecretBox(path.read_bytes().strip())
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)  # create with 600 before the key goes in
    path.write_bytes(key)
    log.warning("Generated a new secret key at %s: back up this file", path)
    return SecretBox(key)


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
