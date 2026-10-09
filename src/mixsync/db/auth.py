import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.core.capabilities import Capability
from mixsync.db.models.auth import Role, RoleCapability, User, UserRole
from mixsync.db.models.auth import Session as SessionRow

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("mixsync-dummy")  # burned on unknown usernames to even out timing


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    capabilities: frozenset[Capability]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_user(s: Session, username: str, password: str, *, admin: bool = False) -> User:
    user = User(username=username, password_hash=hash_password(password), status="active")
    s.add(user)
    s.flush()
    role_id = s.scalars(select(Role.id).where(Role.name == ("admin" if admin else "user"))).one()
    s.add(UserRole(user_id=user.id, role_id=role_id))
    s.flush()
    return user


def authenticate(s: Session, username: str, password: str) -> User | None:
    user = s.scalars(select(User).where(User.username == username)).one_or_none()
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password) or user.status != "active":
        return None
    return user


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(
    s: Session, user_id: int, now: datetime, ttl: timedelta, user_agent: str | None = None
) -> str:
    token = secrets.token_urlsafe(32)
    s.add(
        SessionRow(
            id=_hash_token(token),
            user_id=user_id,
            expires_at=now + ttl,
            last_seen_at=now,
            user_agent=user_agent[:255] if user_agent else None,
        )
    )
    s.flush()
    return token


def user_for_session(s: Session, token: str, now: datetime) -> AuthUser | None:
    row = s.get(SessionRow, _hash_token(token))
    if row is None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires <= now:  # SQLite drops tzinfo; stored values are UTC
        s.delete(row)  # expired sessions are ephemeral rows
        return None
    user = s.get(User, row.user_id)
    if user is None or user.status != "active":
        return None
    row.last_seen_at = now
    caps = s.scalars(
        select(RoleCapability.capability)
        .join(UserRole, UserRole.role_id == RoleCapability.role_id)
        .where(UserRole.user_id == user.id)
    )
    return AuthUser(user.id, user.username, frozenset(Capability(c) for c in caps))


def delete_session(s: Session, token: str) -> None:
    row = s.get(SessionRow, _hash_token(token))
    if row is not None:
        s.delete(row)
