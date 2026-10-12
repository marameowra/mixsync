from typing import Any

import httpx
from pydantic import BaseModel, RootModel, ValidationError

from mixsync.core.errors import AuthError, PermanentError, TransientError
from mixsync.core.types import RecordingRef
from mixsync.ratelimit.client import PoliteClient

# shortcut: the GET metadata endpoint documents no batch cap; 25 mirrors MusicBrainz's inc limit
BATCH = 25
_WEEKLY = "Weekly Exploration"


class _Rec(BaseModel):
    recording_mbid: str


class _Recs(BaseModel):
    class Payload(BaseModel):
        mbids: list[_Rec] = []

    payload: Payload


_PLAYLIST_EXT = "https://musicbrainz.org/doc/jspf#playlist"
_TRACK_EXT = "https://musicbrainz.org/doc/jspf#track"


class _Track(BaseModel):
    identifier: str | list[str] = ""
    title: str = ""
    creator: str = ""
    extension: dict[str, dict[str, Any]] = {}

    def release_hint(self) -> str | None:
        meta: dict[str, Any] = self.extension.get(_TRACK_EXT, {}).get("additional_metadata") or {}
        mbid: str | None = meta.get("caa_release_mbid")
        return mbid


class _Playlist(BaseModel):
    identifier: str = ""
    title: str = ""
    date: str = ""
    track: list[_Track] = []
    extension: dict[str, dict[str, Any]] = {}

    def is_weekly_exploration(self) -> bool:
        meta: dict[str, Any] = (
            self.extension.get(_PLAYLIST_EXT, {}).get("additional_metadata") or {}
        )
        algo: dict[str, Any] = meta.get("algorithm_metadata") or {}
        patch: str | None = algo.get("source_patch")
        return patch == "weekly-exploration" or (patch is None and self.title.startswith(_WEEKLY))


class _PlaylistDoc(BaseModel):
    playlist: _Playlist


class _CreatedFor(BaseModel):
    playlists: list[_PlaylistDoc] = []


class _Radio(BaseModel):
    class Payload(BaseModel):
        jspf: _PlaylistDoc

    payload: Payload


class _Named(BaseModel):
    name: str = ""


class _Release(BaseModel):
    mbid: str | None = None


class _Meta(BaseModel):
    recording: _Named = _Named()
    release: _Release = _Release()
    artist: _Named = _Named()


class _MetaMap(RootModel[dict[str, _Meta]]):
    pass


class _Valid(BaseModel):
    valid: bool = False
    user_name: str | None = None


def _mbid(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _refs(playlist: _Playlist) -> list[RecordingRef]:
    refs: list[RecordingRef] = []
    for t in playlist.track:
        ident = t.identifier if isinstance(t.identifier, str) else next(iter(t.identifier), "")
        if ident:
            refs.append(RecordingRef(_mbid(ident), t.release_hint(), t.creator, t.title))
    return refs


class ListenBrainzClient:
    def __init__(
        self, client: PoliteClient, base_url: str = "https://api.listenbrainz.org"
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def _get[M: BaseModel](
        self, model: type[M], path: str, token: str | None = None, **params: str | int
    ) -> M | None:
        """Parsed body, or None on 204. Messages name the path only, never the token."""
        headers = {"Authorization": f"Token {token}"} if token else {}
        try:
            response = await self._client.get(f"{self._base}{path}", params=params, headers=headers)
        except httpx.TransportError as e:
            raise TransientError(f"ListenBrainz {path} unreachable ({type(e).__name__})") from None
        code = response.status_code
        if code == 401:
            raise AuthError("ListenBrainz rejected the user token; check it in the user's links")
        if code == 204:
            return None
        if 300 <= code < 400:
            raise PermanentError(f"ListenBrainz {path} redirected ({code}); the path is wrong")
        if code == 429 or code >= 500:
            raise TransientError(f"ListenBrainz {path} returned {code}")
        if code != 200:
            raise PermanentError(f"ListenBrainz {path} returned {code}")
        try:
            return model.model_validate_json(response.content)
        except ValidationError:
            raise PermanentError(f"ListenBrainz {path} gave an unusable response") from None

    async def recording_metadata(self, mbids: list[str]) -> dict[str, RecordingRef]:
        out: dict[str, RecordingRef] = {}
        for i in range(0, len(mbids), BATCH):
            body = await self._get(
                _MetaMap,
                "/1/metadata/recording/",  # without the slash the server redirects
                recording_mbids=",".join(mbids[i : i + BATCH]),
                inc="release artist",
            )
            for mbid, m in (body.root if body else {}).items():
                out[mbid] = RecordingRef(mbid, m.release.mbid, m.artist.name, m.recording.name)
        return out

    async def recommendations(self, user: str, token: str, count: int) -> list[RecordingRef]:
        body = await self._get(
            _Recs, f"/1/cf/recommendation/user/{user}/recording", token, count=count
        )
        mbids = [r.recording_mbid for r in body.payload.mbids] if body else []
        found = await self.recording_metadata(mbids)
        return [found[m] for m in mbids if m in found]

    async def weekly_exploration(self, user: str, token: str) -> list[RecordingRef]:
        listing = await self._get(_CreatedFor, f"/1/user/{user}/playlists/createdfor", token)
        weekly = [
            d.playlist
            for d in (listing.playlists if listing else [])
            if d.playlist.is_weekly_exploration()
        ]
        if not weekly:
            return []
        newest = max(weekly, key=lambda p: p.date)
        doc = await self._get(_PlaylistDoc, f"/1/playlist/{_mbid(newest.identifier)}", token)
        return _refs(doc.playlist) if doc else []

    async def lb_radio(self, prompt: str, mode: str, token: str) -> list[RecordingRef]:
        body = await self._get(_Radio, "/1/explore/lb-radio", token, prompt=prompt, mode=mode)
        return _refs(body.payload.jspf.playlist) if body else []

    async def validate_token(self, token: str) -> str | None:
        try:
            body = await self._get(_Valid, "/1/validate-token", token)
        except AuthError:
            return None
        return body.user_name if body and body.valid else None
