from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from mixsync.acquire.merge import enqueue_merge, enqueue_undo
from mixsync.core.capabilities import Capability
from mixsync.db.auth import AuthUser
from mixsync.db.models.library import Plan
from mixsync.library import fragmentation
from mixsync.web.deps import Db, Merge, require
from mixsync.web.templating import templates

router = APIRouter(prefix="/library")
User = Annotated[AuthUser, Depends(require(Capability.library_edit))]

_TYPED_ABOVE = 50  # data-safety rule 5: more file changes than this need the count typed
templates.env.globals["typed_above"] = _TYPED_ABOVE  # pyright: ignore[reportUnknownMemberType]


def _changes(plan: Plan) -> int:
    return sum(op["op"] in ("move", "trash") for op in plan.ops)


def _plan(db: Db, plan_id: int) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "no such plan")
    return plan


def _check_count(plan: Plan, count: str) -> None:
    if _changes(plan) > _TYPED_ABOVE and count.strip() != str(_changes(plan)):
        raise HTTPException(400, f"type {_changes(plan)} to confirm")


@router.get("/fragments")
def fragments(request: Request, user: User, db: Db) -> HTMLResponse:
    plans = db.scalars(select(Plan).order_by(Plan.id.desc()).limit(20)).all()
    return templates.TemplateResponse(
        request,
        "pages/fragments.html",
        {"user": user, "items": fragmentation.find(db), "plans": plans},
    )


@router.post("/fragments/plan")
async def build_plan(
    user: User, db: Db, merger: Merge, group: Annotated[str, Form()]
) -> RedirectResponse:
    db.commit()  # the login's last_seen write must not hold the SQLite lock while build() writes
    try:
        plan_id = await merger.build(group)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: int, request: Request, user: User, db: Db) -> HTMLResponse:
    plan = _plan(db, plan_id)
    counts = {k: sum(op["op"] == k for op in plan.ops) for k in ("move", "trash", "keep")}
    ctx = {"user": user, "plan": plan, "counts": counts, "changes": _changes(plan)}
    return templates.TemplateResponse(request, "pages/plan.html", ctx)


@router.post("/plans/{plan_id}/confirm")
def confirm(
    plan_id: int, user: User, db: Db, count: Annotated[str, Form()] = ""
) -> RedirectResponse:
    plan = _plan(db, plan_id)
    if plan.status != "draft":
        raise HTTPException(409, f"plan is {plan.status}")
    _check_count(plan, count)
    enqueue_merge(db, datetime.now(UTC), plan.id)
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)


@router.post("/plans/{plan_id}/undo")
def undo(plan_id: int, user: User, db: Db, count: Annotated[str, Form()] = "") -> RedirectResponse:
    plan = _plan(db, plan_id)
    if plan.status not in ("done", "failed"):
        raise HTTPException(409, f"plan is {plan.status}")
    _check_count(plan, count)
    enqueue_undo(db, datetime.now(UTC), plan.id)
    return RedirectResponse(f"/library/plans/{plan_id}", status_code=303)
