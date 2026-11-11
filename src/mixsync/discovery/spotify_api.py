"""The Spotify-API upstream (#337): a one-shot import of one of the user's own Spotify playlists,
matched against the local library only. Terms and limits: docs/design/services-etiquette.md
("Spotify", "Rescoped design").

- OAuth Authorization Code with PKCE, no client secret, scope `playlist-read-private` only. The
  refresh token is the user's `spotify` UserLink secret; access tokens stay in memory.
- Spotify is called when the user lists their playlists or presses Import, and by the daily
  `Reimport` pass. An import writes the track list (artist, title, ISRC; nothing else is kept)
  into the mirror's entries; the worker's `Stored` upstream reads them back, so a scheduled sync
  re-resolves and re-pushes without calling Spotify.
- Refresh or delete (Developer Terms IV.3.1, #416): an import goes stale STALE_AFTER its last
  success. Within REIMPORT_WINDOW of that, `Reimport` imports it again in the background, at
  most once a day per mirror. A stale mirror's entries keep only their matches to owned
  tracks, its Navidrome playlist stays as it was, and it is not synced until imported again.
- Missing tracks are only listed: `mirrored.request_missing` refuses this kind.
- Disconnecting deletes the link and the user's mirrors of this kind at once.

API shapes (developer.spotify.com, checked 2026-11-10): /me/playlists items carry `owner.id`,
`collaborative` and `items.total` (older `tracks.total`); /playlists/{id}/items entries carry the
track under `item` (older `track`) and `is_local`."""

import base64
import hashlib
import logging
import random
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from mixsync.core.clock import Clock, aware
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import LIVE, JobKind
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.discovery import MirroredPlaylist
from mixsync.db.models.library import Track
from mixsync.db.models.work import Job
from mixsync.db.queue import JobRecord, enqueue_in
from mixsync.db.secretbox import SecretBox
from mixsync.discovery import mirrored
from mixsync.discovery.mirrored import SPOTIFY_API as KIND
from mixsync.discovery.mirrored import UpstreamTrack
from mixsync.ratelimit.client import PoliteClient

SERVICE = "spotify"  # UserLink.service
AUTHORIZE = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPE = "playlist-read-private"
_ID = re.compile(r"[A-Za-z0-9]{1,64}")  # a Spotify playlist id (base62)
STALE_AFTER = timedelta(days=182)  # 6 months (26 weeks) after the last successful import
REIMPORT_WINDOW = timedelta(days=14)  # background re-imports start this long before stale
REIMPORT_EVERY = timedelta(days=1)  # at most one background attempt per mirror per day
STALE = "the Spotify import is stale; import it again"

log = logging.getLogger(__name__)


class Revoked(PermanentError):
    """Spotify refused the refresh token (invalid_grant): the user must connect again."""


def pkce() -> tuple[str, str]:
    """A code verifier and its S256 challenge (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)  # 86 unreserved characters, within 43-128
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    return f"{AUTHORIZE}?{urlencode(query)}"


def _obj(x: object) -> dict[str, Any]:
    return cast(dict[str, Any], x) if isinstance(x, dict) else {}


def _list(x: object) -> list[Any]:
    return cast(list[Any], x) if isinstance(x, list) else []


@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    total: int


def track_of(entry: object) -> UpstreamTrack | None:
    """One /playlists/{id}/items entry; None for a local file, an episode or a removed track."""
    e = _obj(entry)
    t = _obj(e.get("item") or e.get("track"))
    if e.get("is_local") or t.get("is_local") or t.get("type") != "track":
        return None
    artist = str(_obj(next(iter(_list(t.get("artists"))), None)).get("name") or "").strip()
    title = str(t.get("name") or "").strip()
    isrc = str(_obj(t.get("external_ids")).get("isrc") or "").strip().upper()
    return UpstreamTrack(artist, title, isrc or None) if artist and title else None


class SpotifyClient:
    """Spotify Web API calls through the polite client. Tokens are only ever sent, never put in
    an error, a URL or a log line."""

    def __init__(self, http: PoliteClient, client_id: str) -> None:
        self.http, self.client_id = http, client_id

    async def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            r = await self.http.request(method, url, **kwargs)
        except httpx.TransportError:
            raise TransientError("Spotify unreachable") from None
        path = httpx.URL(url).path
        if r.status_code >= 500:
            raise TransientError(f"Spotify {path} returned {r.status_code}")
        try:
            body = _obj(r.json())
        except ValueError:
            body = {}
        if url == TOKEN and body.get("error") == "invalid_grant":
            raise Revoked("Spotify refused the stored login; connect Spotify again")
        if r.status_code != 200:
            raise PermanentError(f"Spotify {path} returned {r.status_code}")
        return body

    async def _token(self, form: dict[str, str]) -> tuple[str, str | None]:
        body = await self._call("POST", TOKEN, data={**form, "client_id": self.client_id})
        if not body.get("access_token"):
            raise PermanentError("Spotify returned no access token")
        return str(body["access_token"]), body.get("refresh_token") or None

    async def exchange(self, code: str, redirect_uri: str, verifier: str) -> tuple[str, str]:
        """(access token, refresh token) for an authorization code."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        access, refresh = await self._token(form)
        if refresh is None:
            raise PermanentError("Spotify returned no refresh token")
        return access, refresh

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        """(access token, refresh token); Spotify may keep the old refresh token. Raises Revoked."""
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        access, new = await self._token(form)
        return access, new or refresh_token

    async def _get(self, access: str, url: str) -> dict[str, Any]:
        return await self._call("GET", url, headers={"Authorization": f"Bearer {access}"})

    async def _pages(self, access: str, url: str) -> list[Any]:
        """Every item of a paged list, following `next` (only ever to the API itself)."""
        out: list[Any] = []
        next_url: object = url
        while isinstance(next_url, str) and next_url.startswith(API + "/"):
            page = await self._get(access, next_url)
            out += _list(page.get("items"))
            next_url = page.get("next")
        return out

    async def me(self, access: str) -> str:
        """The Spotify user id the token belongs to."""
        uid = str((await self._get(access, f"{API}/me")).get("id") or "")
        if not uid:
            raise PermanentError("Spotify returned no user id")
        return uid

    async def playlists(self, access: str, me: str) -> list[Playlist]:
        """The user's playlists they own or that are collaborative; Spotify hides the items of
        any other playlist from a development-mode app."""
        out: list[Playlist] = []
        for raw in await self._pages(access, f"{API}/me/playlists?limit=50"):
            p = _obj(raw)
            pid, name = str(p.get("id") or ""), str(p.get("name") or "").strip()
            mine = _obj(p.get("owner")).get("id") == me or p.get("collaborative") is True
            if mine and _ID.fullmatch(pid) and name:
                total = _obj(p.get("items")).get("total", _obj(p.get("tracks")).get("total"))
                out.append(Playlist(pid, name, total if isinstance(total, int) else 0))
        return out

    async def tracks(self, access: str, playlist_id: str) -> list[UpstreamTrack]:
        """The playlist's tracks in order, local files and episodes left out."""
        if not _ID.fullmatch(playlist_id):
            raise PermanentError("not a Spotify playlist id")
        url = f"{API}/playlists/{playlist_id}/items?limit=50"
        return [t for e in await self._pages(access, url) if (t := track_of(e))]


def save(
    s: Session, now: datetime, user_id: int, ref: str, name: str, tracks: list[UpstreamTrack]
) -> MirroredPlaylist:
    """The user's mirror of Spotify playlist `ref`, made on the first import, with `tracks` as
    its list (a track matched before keeps its match) and a sync queued now. A name the user
    already uses for another mirror raises IntegrityError."""
    pl = mirrored.create(s, now, user_id, name[:255], KIND, ref)
    known = {mirrored.key(e): e for e in pl.entries if e.get("recording_mbid")}
    pl.entries = [
        known.get((t.artist, t.title, t.isrc))
        or {"artist": t.artist, "title": t.title, "isrc": t.isrc, "recording_mbid": None}
        for t in tracks
    ]
    pl.imported_at, pl.reimport_after = now, now + STALE_AFTER - REIMPORT_WINDOW
    mirrored.sync_now(s, now, pl, force=True)
    return pl


def stale_at(pl: MirroredPlaylist) -> datetime | None:
    return aware(pl.imported_at) + STALE_AFTER if pl.imported_at else None


def is_stale(pl: MirroredPlaylist, now: datetime) -> bool:
    at = stale_at(pl)
    return at is not None and now >= at


def strip(s: Session, pl: MirroredPlaylist) -> None:
    """Drop Spotify's data from a stale mirror: only entries matched to an owned track stay,
    with their MusicBrainz match and nothing from Spotify."""
    mbids = [e["recording_mbid"] for e in pl.entries if e.get("recording_mbid")]
    owned = set(s.scalars(select(Track.recording_mbid).where(Track.recording_mbid.in_(mbids))))
    fields = ("recording_mbid", "release_mbid", "rec_artist", "rec_title")
    pl.entries = [
        {f: e.get(f) for f in fields} for e in pl.entries if e.get("recording_mbid") in owned
    ]


def _link(s: Session, user_id: int) -> UserLink | None:
    return s.scalar(
        select(UserLink).where(UserLink.user_id == user_id, UserLink.service == SERVICE)
    )


async def access_token(
    client: SpotifyClient,
    sessions: sessionmaker[Session],
    box: SecretBox,
    user_id: int,
    refresh: str,
) -> str:
    """An access token for the user's stored refresh token. A rotated refresh token is stored;
    a refused one marks the link for reconnecting and raises Revoked."""
    try:
        access, new = await client.refresh(refresh)
    except Revoked:
        access, new = "", ""
    if new != refresh:
        with sessions.begin() as s:
            if link := _link(s, user_id):
                link.secret_encrypted = box.encrypt(new) if new else ""  # "" never decrypts
                link.needs_relink = not new
    if not access:
        raise Revoked("Spotify refused the stored login; reconnect Spotify")
    return access


def disconnect(s: Session, user_id: int) -> None:
    """Delete the user's Spotify login and every mirror imported from Spotify. Files and the
    playlists already in Navidrome are left alone; a queued sync finds no row and stops."""
    s.execute(delete(UserLink).where(UserLink.user_id == user_id, UserLink.service == SERVICE))
    s.execute(
        delete(MirroredPlaylist).where(
            MirroredPlaylist.user_id == user_id, MirroredPlaylist.upstream_kind == KIND
        )
    )


class Stored:
    """The worker's upstream for this kind: the list the last import stored. Never calls
    Spotify. A stale import is not synced."""

    def __init__(self, sessions: sessionmaker[Session], clock: Clock) -> None:
        self.sessions, self.clock = sessions, clock

    async def tracks(self, user_id: int, ref: str) -> list[UpstreamTrack]:
        with self.sessions() as s:
            pl = s.scalar(
                select(MirroredPlaylist).where(
                    MirroredPlaylist.user_id == user_id,
                    MirroredPlaylist.upstream_kind == KIND,
                    MirroredPlaylist.upstream_ref == ref,
                )
            )
            if pl is None:
                return []
            if is_stale(pl, self.clock.now()):
                raise PermanentError(STALE)
            return [UpstreamTrack(e["artist"], e["title"], e.get("isrc")) for e in pl.entries]


class Reimport:
    """The daily pass (#416): import each mirror due for it again from Spotify, as Import again
    does, then strip the mirrors gone stale. It only replaces mirror entries: nothing here
    makes a Request or reaches a download source."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Clock,
        box: SecretBox,
        client: SpotifyClient | None,  # None: no Spotify client ID, so no background calls
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.sessions, self.clock, self.box, self.client, self.rng = (
            sessions,
            clock,
            box,
            client,
            rng,
        )

    def handlers(self) -> dict[JobKind, Callable[[JobRecord], Awaitable[None]]]:
        return {JobKind.SPOTIFY_REIMPORT: self.run}

    def start_chains(self) -> None:
        """On startup: queue a pass unless a chain is already live."""
        with self.sessions.begin() as s:
            live = s.scalar(
                select(Job.id)
                .where(Job.kind == JobKind.SPOTIFY_REIMPORT, Job.status.in_(LIVE))
                .limit(1)
            )
            if live is None:
                enqueue_in(s, self.clock.now(), JobKind.SPOTIFY_REIMPORT, {})

    async def run(self, job: JobRecord) -> None:
        try:
            await self.reimport_due()
            self.strip_stale()
        finally:  # the chain goes on after a failure too; the key makes a retry add no second
            now = self.clock.now()
            with self.sessions.begin() as s:
                enqueue_in(
                    s,
                    now,
                    JobKind.SPOTIFY_REIMPORT,
                    {},
                    run_after=now + REIMPORT_EVERY * self.rng(0.9, 1.1),
                    idempotency_key=f"spotify-reimport:after:{job.id}",
                )

    async def reimport_due(self) -> None:
        if self.client is None:
            return
        now = self.clock.now()
        with self.sessions.begin() as s:
            due = list(
                s.scalars(
                    select(MirroredPlaylist).where(
                        MirroredPlaylist.upstream_kind == KIND,
                        MirroredPlaylist.reimport_after <= now,
                    )
                )
            )
            for pl in due:  # claimed before any call: one attempt a day, whatever happens next
                pl.reimport_after = now + REIMPORT_EVERY
            todo = [(pl.id, pl.user_id, pl.upstream_ref) for pl in due]
        for pid, uid, ref in todo:
            await self._reimport(self.client, pid, uid, ref)

    async def _reimport(self, client: SpotifyClient, pid: int, uid: int, ref: str) -> None:
        with self.sessions() as s:
            link = _link(s, uid)
            refresh = self.box.decrypt(link.secret_encrypted) if link else None
        if not refresh:  # not connected, or must reconnect: it goes stale on schedule
            return
        try:
            access = await access_token(client, self.sessions, self.box, uid, refresh)
        except (TransientError, PermanentError) as e:  # Revoked set needs_relink; else tomorrow
            log.warning("spotify: re-importing playlist %s: %s", pid, e)
            return
        try:
            tracks = await client.tracks(access, ref)
        except TransientError as e:
            log.warning("spotify: re-importing playlist %s: %s; trying tomorrow", pid, e)
            return
        except PermanentError as e:  # 403/404: deleted, or no longer theirs; never retried
            log.warning("spotify: re-importing playlist %s: %s; not retried", pid, e)
            with self.sessions.begin() as s:
                if pl := s.get(MirroredPlaylist, pid):
                    pl.reimport_after = None
            return
        if not tracks:  # emptied on Spotify: keep the last list, as a sync does
            return
        with self.sessions.begin() as s:
            if pl := s.get(MirroredPlaylist, pid):
                save(s, self.clock.now(), uid, ref, pl.name, tracks)
        log.info("spotify: re-imported playlist %s in the background", pid)

    def strip_stale(self) -> None:
        with self.sessions.begin() as s:
            stale = s.scalars(
                select(MirroredPlaylist).where(
                    MirroredPlaylist.upstream_kind == KIND,
                    MirroredPlaylist.imported_at <= self.clock.now() - STALE_AFTER,
                )
            ).all()
            for pl in stale:
                strip(s, pl)
