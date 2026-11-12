from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from mixsync.db.models.auth import User
from mixsync.db.models.work import Request


def test_removed_request_id_is_not_reused(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as db:
        user = User(username="u", password_hash="x", status="active")
        db.add(user)
        db.flush()
        first = Request(user_id=user.id, release_mbid="a")
        db.add(first)
        db.flush()
        first_id = first.id
        db.execute(delete(Request).where(Request.id == first_id))
        second = Request(user_id=user.id, release_mbid="b")
        db.add(second)
        db.flush()
        assert second.id != first_id
