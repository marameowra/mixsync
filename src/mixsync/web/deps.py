import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from mixsync.acquire.merge import Merger
from mixsync.core.capabilities import Capability
from mixsync.core.clock import SystemClock
from mixsync.core.protocols import ReleaseWatch
from mixsync.db import decisions
from mixsync.db.auth import AuthUser, user_for_session
from mixsync.db.journal import SqlJournal
from mixsync.db.models.matching import MatchDecision
from mixsync.db.secretbox import SecretBox, load_box
from mixsync.library.fileops import FileOps
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient, polite_client

SESSION_COOKIE = "mixsync_session"


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as s, s.begin():
        yield s


# Commit before the response goes out: a streamed preview would otherwise hold the write lock
# (every request writes last_seen_at) for the whole playback, locking out the worker.
Db = Annotated[Session, Depends(get_db, scope="function")]


def get_merger(request: Request) -> Merger:
    """Built on first use. MusicBrainz goes through the same DB-backed bucket and cache as the
    worker's, so the rate limit holds across both processes. No FileOps.recover() here."""
    state = request.app.state
    if getattr(state, "merger", None) is None:
        st, sessions, clock = state.settings, state.session_factory, SystemClock()
        fileops = FileOps(
            Path(st.data_dir), Path(st.downloads_dir), SqlJournal(sessions, clock), clock
        )
        state.merger = Merger(
            sessions,
            clock,
            MusicBrainzProvider(polite_client("musicbrainz", st, sessions)),
            fileops,
            st.path_template,
            tuple(st.canonical_media),
            tuple(st.canonical_countries),
            st.trash_days,
        )
    merger: Merger = state.merger
    return merger


Merge = Annotated[Merger, Depends(get_merger)]


def get_watch(request: Request) -> ReleaseWatch:
    """MusicBrainz for follow search and lookup, on the same bucket and cache as the worker's."""
    state = request.app.state
    if getattr(state, "watch", None) is None:
        state.watch = MusicBrainzProvider(
            polite_client("musicbrainz", state.settings, state.session_factory)
        )
    watch: ReleaseWatch = state.watch
    return watch


Watch = Annotated[ReleaseWatch, Depends(get_watch)]


_box_lock = threading.Lock()


def get_box(request: Request) -> SecretBox:
    """Loaded on first use; a key file with loose permissions fails here, before any secret is
    read or written."""
    state = request.app.state
    with _box_lock:  # sync dependencies run in a thread pool
        if getattr(state, "secret_box", None) is None:
            state.secret_box = load_box(state.settings)
        box: SecretBox = state.secret_box
    return box


Box = Annotated[SecretBox, Depends(get_box)]


def get_navidrome_http(request: Request) -> PoliteClient:
    """One client for the app's life; account links ping Navidrome as the user through it."""
    state = request.app.state
    if getattr(state, "navidrome_http", None) is None:
        state.navidrome_http = polite_client("navidrome", state.settings, state.session_factory)
    http: PoliteClient = state.navidrome_http
    return http


def get_listenbrainz_http(request: Request) -> PoliteClient:
    state = request.app.state
    if getattr(state, "listenbrainz_http", None) is None:
        state.listenbrainz_http = polite_client(
            "listenbrainz", state.settings, state.session_factory
        )
    http: PoliteClient = state.listenbrainz_http
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
