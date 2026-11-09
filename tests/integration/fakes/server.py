"""Fake slskd, MusicBrainz, AcoustID and Cover Art Archive for the compose suite (ADR 0016).

One HTTP server on :5030. mixsync reaches it as slskd (MIXSYNC_SLSKD_URL), as MusicBrainz
(MIXSYNC_MB_BASE_URL) and as AcoustID and the Cover Art Archive
(MIXSYNC_TEST_ONLY_FAKE_SERVICES_URL). It serves the canned albums in ALBUMS: a search returns
one peer per matching album, an enqueue copies that album's generated FLACs into the downloads
volume the way slskd saves them, and an AcoustID lookup names a recording by the file's length.

Response shapes follow slskd 0.26 (the version in deploy/compose.yml) as `SlskdSource` reads them.
"""

import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

DOWNLOADS = Path("/downloads")
FIXTURES = Path("/tmp/fixtures")
ARTIST = "Integration Artist"
ARTIST_ID = "1a000000-0000-4000-8000-000000000001"


def _album(n: int, title: str, peer: str, lengths: list[int], *, lossy: bool) -> dict[str, Any]:
    return {
        "id": f"2b000000-0000-4000-8000-00000000000{n}",
        "group": f"3c000000-0000-4000-8000-00000000000{n}",
        "title": title,
        "peer": peer,
        "folder": f"{ARTIST} - {title} (2020)",
        # a low-passed copy looks like a lossy transcode: the matcher vetoes it into review
        "lossy": lossy,
        "tracks": [
            {
                "title": f"{title} Song {i}",
                "length": s,  # seconds; unique across ALBUMS, AcoustID is faked by length
                "recording": f"4d000000-0000-4000-8000-0000000000{n}{i}",
            }
            for i, s in enumerate(lengths, 1)
        ],
    }


ALBUMS = [
    _album(1, "Happy Path", "happy-peer", [7, 8, 9], lossy=False),
    _album(2, "Review Band", "review-peer", [10, 11, 12], lossy=True),
]


def _remote(album: dict[str, Any], i: int) -> str:
    track = album["tracks"][i]
    return f"@@music\\{ARTIST}\\{album['folder']}\\{i + 1:02} - {track['title']}.flac"


def _fixture(album: dict[str, Any], i: int) -> Path:
    return FIXTURES / album["peer"] / _remote(album, i).rpartition("\\")[2]


def generate() -> None:
    """Short noise FLACs with plain tags, made once at startup (nothing large is committed)."""
    for a in ALBUMS:
        for i, t in enumerate(a["tracks"]):
            out = _fixture(a, i)
            out.parent.mkdir(parents=True, exist_ok=True)
            filters = ["aresample=8000", "aresample=44100"] if a["lossy"] else []
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
                + ["-f", "lavfi", "-i", f"anoisesrc=d={t['length']}:a=0.3:seed={i + 1}"]
                + ["-ar", "44100", "-ac", "2", "-sample_fmt", "s16"]
                + (["-af", ",".join(filters)] if filters else [])
                + ["-metadata", f"title={t['title']}", "-metadata", f"artist={ARTIST}"]
                + ["-metadata", f"album_artist={ARTIST}", "-metadata", f"album={a['title']}"]
                + ["-metadata", f"track={i + 1}/{len(a['tracks'])}", "-metadata", "date=2020"]
                + ["-c:a", "flac", str(out)],
                check=True,
            )


def _credit() -> list[dict[str, Any]]:
    return [{"name": ARTIST, "joinphrase": "", "artist": {"id": ARTIST_ID, "name": ARTIST}}]


def mb_release(a: dict[str, Any]) -> dict[str, Any]:
    """The fields MusicBrainzProvider.get_release reads, in MusicBrainz's ws/2 JSON shape."""
    return {
        "id": a["id"],
        "title": a["title"],
        "status": "Official",
        "date": "2020-01-01",
        "country": "XW",
        "disambiguation": "",
        "artist-credit": _credit(),
        "release-group": {
            "id": a["group"],
            "title": a["title"],
            "primary-type": "Album",
            "secondary-types": [],
            "first-release-date": "2020-01-01",
            "artist-credit": _credit(),
        },
        "label-info": [],
        "media": [
            {
                "position": 1,
                "format": "Digital Media",
                "track-count": len(a["tracks"]),
                "tracks": [
                    {
                        "title": t["title"],
                        "position": i,
                        "length": t["length"] * 1000,
                        "recording": {
                            "id": t["recording"],
                            "title": t["title"],
                            "length": t["length"] * 1000,
                            "artist-credit": _credit(),
                        },
                    }
                    for i, t in enumerate(a["tracks"], 1)
                ],
            }
        ],
    }


app = FastAPI()
searches: dict[str, dict[str, Any]] = {}
transfers: dict[str, list[dict[str, Any]]] = {}  # username -> transfer records


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- slskd -----------------------------------------------------------------------------------


@app.get("/api/v0/application")
def application() -> dict[str, Any]:
    return {"version": {"current": "0.26.2-fake"}, "server": {"isConnected": True}}


@app.post("/api/v0/searches")
async def create_search(request: Request) -> dict[str, Any]:
    body = await request.json()
    text = body["searchText"].casefold()
    hits = [a for a in ALBUMS if a["title"].casefold() in text]
    searches[body["id"]] = {"text": body["searchText"], "hits": hits}
    return _search_state(body["id"])


def _search_state(search_id: str) -> dict[str, Any]:
    s = searches[search_id]
    return {
        "id": search_id,
        "searchText": s["text"],
        "isComplete": True,
        "state": "Completed, TimedOut",
        "responseCount": len(s["hits"]),
        "fileCount": sum(len(a["tracks"]) for a in s["hits"]),
        "lockedFileCount": 0,
        "startedAt": _now(),
    }


@app.get("/api/v0/searches/{search_id}", response_model=None)
def get_search(search_id: str) -> dict[str, Any] | Response:
    return _search_state(search_id) if search_id in searches else Response(status_code=404)


@app.delete("/api/v0/searches/{search_id}")
def delete_search(search_id: str) -> Response:
    return Response(status_code=204 if searches.pop(search_id, None) else 404)


@app.get("/api/v0/searches/{search_id}/responses", response_model=None)
def search_responses(search_id: str) -> list[dict[str, Any]] | Response:
    if search_id not in searches:
        return Response(status_code=404)
    return [
        {
            "username": a["peer"],
            "token": 1,
            "hasFreeUploadSlot": True,
            "queueLength": 0,
            "uploadSpeed": 5_000_000,
            "fileCount": len(a["tracks"]),
            "lockedFileCount": 0,
            "lockedFiles": [],
            "files": [
                {
                    "code": 1,
                    "filename": _remote(a, i),
                    "size": _fixture(a, i).stat().st_size,
                    "extension": "flac",
                    "bitDepth": 16,
                    "sampleRate": 44100,
                    "length": t["length"],
                    "isLocked": False,
                }
                for i, t in enumerate(a["tracks"])
            ],
        }
        for a in searches[search_id]["hits"]
    ]


@app.post("/api/v0/transfers/downloads/{username}")
async def enqueue(username: str, request: Request) -> JSONResponse:
    """Finishes at once: the file is copied where slskd would save it (its parent folder's
    name under /downloads) and the transfer is recorded as Completed, Succeeded."""
    by_name = {
        _remote(a, i): _fixture(a, i)
        for a in ALBUMS
        if a["peer"] == username
        for i in range(len(a["tracks"]))
    }
    enqueued, failed = [], []
    for f in await request.json():
        src = by_name.get(f["filename"])
        if src is None:
            failed.append(f)
            continue
        folder, _, name = f["filename"].rpartition("\\")
        dest = DOWNLOADS / folder.rpartition("\\")[2] / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        record = {
            "id": str(uuid.uuid4()),
            "username": username,
            "direction": "Download",
            "filename": f["filename"],
            "size": f["size"],
            "state": "Completed, Succeeded",
            "requestedAt": _now(),
            "bytesTransferred": src.stat().st_size,
            "percentComplete": 100,
        }
        transfers.setdefault(username, []).append(record)
        enqueued.append(record)
    return JSONResponse({"enqueued": enqueued, "failed": failed}, status_code=201)


def _user(username: str) -> dict[str, Any]:
    dirs: dict[str, list[dict[str, Any]]] = {}
    for t in transfers[username]:
        dirs.setdefault(t["filename"].rpartition("\\")[0], []).append(t)
    return {
        "username": username,
        "directories": [
            {"directory": d, "fileCount": len(fs), "files": fs} for d, fs in dirs.items()
        ],
    }


@app.get("/api/v0/transfers/downloads")
def all_downloads() -> list[dict[str, Any]]:
    return [_user(u) for u in transfers if transfers[u]]


@app.get("/api/v0/transfers/downloads/{username}", response_model=None)
def user_downloads(username: str) -> dict[str, Any] | Response:
    return _user(username) if transfers.get(username) else Response(status_code=404)


@app.delete("/api/v0/transfers/downloads/{username}/{transfer_id}")
def remove_download(username: str, transfer_id: str) -> Response:
    kept = [t for t in transfers.get(username, []) if t["id"] != transfer_id]
    found = len(kept) != len(transfers.get(username, []))
    transfers[username] = kept
    return Response(status_code=204 if found else 404)


# --- MusicBrainz, AcoustID, Cover Art Archive ---------------------------------------------------


@app.get("/ws/2/release/{mbid}", response_model=None)
def release(mbid: str) -> dict[str, Any] | JSONResponse:
    for a in ALBUMS:
        if a["id"] == mbid:
            return mb_release(a)
    return JSONResponse({"error": "Not Found"}, status_code=404)


@app.get("/ws/2/release")
def browse_releases(
    release_group: str = Query("", alias="release-group"), recording: str = ""
) -> dict[str, Any]:
    """Browse by release group or recording; a search (`query=`) finds nothing."""
    found = [
        a
        for a in ALBUMS
        if release_group == a["group"] or recording in {t["recording"] for t in a["tracks"]}
    ]
    releases = [
        {
            **{k: v for k, v in mb_release(a).items() if k != "media"},
            "media": [{"format": "Digital Media", "track-count": len(a["tracks"])}],
        }
        for a in found
    ]
    return {"release-count": len(releases), "release-offset": 0, "releases": releases}


@app.get("/v2/lookup")
def acoustid_lookup(duration: int = 0) -> dict[str, Any]:
    hits = [
        {
            "id": f"5e000000-0000-4000-8000-{t['length']:012}",
            "score": 0.98,
            "recordings": [{"id": t["recording"], "title": t["title"], "duration": t["length"]}],
        }
        for a in ALBUMS
        for t in a["tracks"]
        if t["length"] == duration
    ]
    return {"status": "ok", "results": hits}


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
def anything_else(path: str) -> JSONResponse:
    """No cover art and no other MusicBrainz data: mixsync treats both as absent."""
    print(f"fake: unhandled /{path}", file=sys.stderr, flush=True)
    return JSONResponse({"error": "Not Found"}, status_code=404)


if __name__ == "__main__":
    generate()
    uvicorn.run(app, host="0.0.0.0", port=5030, log_level="warning")
