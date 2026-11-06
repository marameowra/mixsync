from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mixsync.core.capabilities import Capability
from mixsync.db.auth import (
    AuthUser,
    create_user,
    delete_user_sessions,
    is_admin,
    other_active_admins,
    set_password,
    set_role,
)
from mixsync.db.models.auth import User
from mixsync.web.deps import Db, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/users")
Admin = Annotated[AuthUser, Depends(require(Capability.admin))]
MAX_LEN = 200
LAST = "At least one active administrator must remain."


def _page(request: Request, admin: AuthUser, s: Session, error: str | None = None) -> HTMLResponse:
    users = s.scalars(select(User).order_by(User.username)).all()
    rows = [(u, is_admin(s, u.id)) for u in users]
    return templates.TemplateResponse(
        request,
        "pages/users.html",
        {"user": admin, "rows": rows, "error": error},
        status_code=400 if error else 200,
    )


def _target(db: Session, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "no such user")
    return target


def _last_admin(db: Session, target: User) -> bool:
    """True when the target is the only active admin: it can't be demoted or disabled."""
    return (
        target.status == "active"
        and is_admin(db, target.id)
        and other_active_admins(db, target.id) == 0
    )


@router.get("")
def show(request: Request, admin: Admin, db: Db) -> HTMLResponse:
    return _page(request, admin, db)


@router.post("")
def add(
    request: Request,
    admin: Admin,
    db: Db,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    make_admin: Annotated[bool, Form(alias="admin")] = False,
) -> Response:
    username = username.strip()
    if not username or not password or len(username) > MAX_LEN or len(password) > MAX_LEN:
        return _page(request, admin, db, "Enter a username and a password.")
    exists = f"{username} already exists."
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        return _page(request, admin, db, exists)
    try:
        with db.begin_nested():
            create_user(db, username, password, admin=make_admin)
    except IntegrityError:  # a double submit lost the race
        return _page(request, admin, db, exists)
    return RedirectResponse("/users", 303)


@router.post("/{user_id}/status")
def set_status(
    request: Request, user_id: int, admin: Admin, db: Db, status: Annotated[str, Form()]
) -> Response:
    target = _target(db, user_id)
    if status not in ("active", "disabled"):
        raise HTTPException(422, "bad status")
    if status == "disabled" and _last_admin(db, target):
        return _page(request, admin, db, LAST)
    target.status = status
    if status == "disabled":
        delete_user_sessions(db, target.id)  # re-enabling must not revive old sessions
    return RedirectResponse("/users", 303)


@router.post("/{user_id}/role")
def change_role(
    request: Request, user_id: int, admin: Admin, db: Db, role: Annotated[str, Form()]
) -> Response:
    target = _target(db, user_id)
    if role not in ("admin", "user"):
        raise HTTPException(422, "bad role")
    if role == "user" and _last_admin(db, target):
        return _page(request, admin, db, LAST)
    set_role(db, target.id, role == "admin")
    return RedirectResponse("/users", 303)


@router.post("/{user_id}/password")
def reset_password(
    request: Request, user_id: int, admin: Admin, db: Db, password: Annotated[str, Form()] = ""
) -> Response:
    target = _target(db, user_id)
    if not password or len(password) > MAX_LEN:
        return _page(request, admin, db, "Enter a new password.")
    set_password(db, target, password)  # an admin resetting their own password is logged out too
    return RedirectResponse("/users", 303)
