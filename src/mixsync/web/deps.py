from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mixsync.core.capabilities import Capability
from mixsync.db import decisions
from mixsync.db.auth import AuthUser, user_for_session
from mixsync.db.models.matching import MatchDecision

SESSION_COOKIE = "mixsync_session"


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as s, s.begin():
        yield s


Db = Annotated[Session, Depends(get_db)]


def current_user(request: Request, db: Db) -> AuthUser:
    token = request.cookies.get(SESSION_COOKIE)
    user = user_for_session(db, token, datetime.now(UTC)) if token else None
    if user is None:
        raise HTTPException(303, headers={"Location": "/login"})
    return user


CurrentUser = Annotated[AuthUser, Depends(current_user)]


def decision_for(db: Session, user: AuthUser, decision_id: int) -> MatchDecision:
    """Own items need `request`; anyone's need `approve`."""
    d = decisions.get(db, decision_id)
    if d is None:
        raise HTTPException(404, "no such decision")
    if d.user_id != user.id and Capability.approve not in user.capabilities:
        raise HTTPException(403, "missing capability: approve")
    return d


def require(cap: Capability) -> Callable[..., AuthUser]:
    def dep(user: CurrentUser) -> AuthUser:
        if cap not in user.capabilities:
            raise HTTPException(403, f"missing capability: {cap.value}")
        return user

    return dep
