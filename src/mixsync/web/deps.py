from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mixsync.acquire.merge import Merger
from mixsync.core.capabilities import Capability
from mixsync.core.protocols import NameLookup, ReleaseWatch
from mixsync.db.auth import AuthUser, user_for_session
from mixsync.db.models.matching import MatchDecision
from mixsync.db.secretbox import SecretBox
from mixsync.library.fileops import FileOps
from mixsync.metadata.coverart import CoverArtClient
from mixsync.ratelimit.client import PoliteClient

SESSION_COOKIE = "mixsync_session"


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as s, s.begin():
        yield s


# Commit before the response goes out: a streamed preview would otherwise hold the write lock
# (every request writes last_seen_at) for the whole playback, locking out the worker.
Db = Annotated[Session, Depends(get_db, scope="function")]


def get_fileops(request: Request) -> FileOps:
    fileops: FileOps = request.app.state.services.fileops
    return fileops


def get_merger(request: Request) -> Merger:
    merger: Merger = request.app.state.services.merger
    return merger


Merge = Annotated[Merger, Depends(get_merger)]
Files = Annotated[FileOps, Depends(get_fileops)]


def get_watch(request: Request) -> ReleaseWatch:
    """The one MusicBrainzProvider, for follow search and lookup."""
    watch: ReleaseWatch = request.app.state.services.musicbrainz
    return watch


Watch = Annotated[ReleaseWatch, Depends(get_watch)]


def get_namer(request: Request) -> NameLookup:
    """The same provider, which also names ids."""
    namer: NameLookup = request.app.state.services.musicbrainz
    return namer


Namer = Annotated[NameLookup, Depends(get_namer)]


def get_box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.services.secret_box
    return box


Box = Annotated[SecretBox, Depends(get_box)]


def get_navidrome_http(request: Request) -> PoliteClient:
    """Account links ping Navidrome as the user through it."""
    http: PoliteClient = request.app.state.services.navidrome_http
    return http


def get_coverart(request: Request) -> CoverArtClient:
    cover: CoverArtClient = request.app.state.services.coverart
    return cover


Cover = Annotated[CoverArtClient, Depends(get_coverart)]


def get_listenbrainz_http(request: Request) -> PoliteClient:
    http: PoliteClient = request.app.state.services.listenbrainz_http
    return http


def current_user(request: Request, db: Db) -> AuthUser:
    token = request.cookies.get(SESSION_COOKIE)
    user = user_for_session(db, token, datetime.now(UTC)) if token else None
    if user is None:
        raise HTTPException(303, headers={"Location": "/login"})
    return user


CurrentUser = Annotated[AuthUser, Depends(current_user)]


def decision_for(db: Session, user: AuthUser, decision_id: int) -> MatchDecision:
    """Own items need `request`; anyone's need `approve`."""
    d = db.get(MatchDecision, decision_id)
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
