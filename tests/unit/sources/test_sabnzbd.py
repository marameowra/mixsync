"""SABnzbd's API. The cassettes follow SABnzbd's official API docs (queue, history, addfile);
they were written to that schema, not recorded from a live instance."""

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from mixsync.core.errors import AuthError, PermanentError
from mixsync.core.matching import Candidate, CandidateFile, TransferStatus
from mixsync.ratelimit.client import PoliteClient
from mixsync.sources.sabnzbd import CATEGORY, Sabnzbd, job_name

CASSETTES = Path(__file__).parents[2] / "cassettes" / "sabnzbd"
BASE = "http://sabnzbd:8080"
KEY = "sab-secret-key"
HASH = "5f1e" * 10
OURS, USERS = "SABnzbd_nzo_kyt1f0", "SABnzbd_nzo_p86tgx"
TITLE = "Test.Artist-Test.Album-WEB-2020-GRP"
CAND = Candidate(
    "ExampleUsenet",
    TITLE,
    (
        CandidateFile(f"{TITLE}\\01 - Opening.flac", 30_600_000, "flac"),
        CandidateFile(f"{TITLE}\\02 - Middle.flac", 31_600_000, "flac"),
    ),
    source="prowlarr",
    nzb_hash=HASH,
)

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]


def form(request: httpx.Request) -> dict[str, str]:
    """The POSTed fields, url-encoded or multipart (the file part left out)."""
    if "multipart" in request.headers["content-type"]:
        fields = re.findall(rb'name="(\w+)"\r\n\r\n([^\r]*)\r\n', request.content)
        return {k.decode(): v.decode() for k, v in fields}
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


class FakeSab:
    """SABnzbd's /api from the cassettes. It does not filter by category or search: the client
    must, and the cassettes hold a user's job of the same name in category `tv`."""

    def __init__(self, done: Path | None = None) -> None:
        self.queue = json.loads((CASSETTES / "queue.json").read_text())
        self.history = json.loads((CASSETTES / "history.json").read_text())
        self.history["history"]["slots"][0]["storage"] = str(done) if done else "/usenet/x"
        self.seen: list[httpx.Request] = []
        self.calls: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        f = form(request)
        if f.get("apikey") != KEY:
            return httpx.Response(200, json={"status": False, "error": "API Key Incorrect"})
        self.calls.append(f)
        if f["mode"] == "addfile":
            return httpx.Response(200, json=json.loads((CASSETTES / "addfile.json").read_text()))
        if f.get("name") == "delete":
            return httpx.Response(200, json={"status": True, "nzo_ids": [f["value"]]})
        return httpx.Response(200, json=self.queue if f["mode"] == "queue" else self.history)

    def drop(self, mode: str, nzo_id: str) -> None:
        slots = (self.queue["queue"] if mode == "queue" else self.history["history"])["slots"]
        slots[:] = [s for s in slots if s["nzo_id"] != nzo_id]

    def deletes(self) -> list[dict[str, str]]:
        return [c for c in self.calls if c.get("name") == "delete"]


def sab(make_client: Make, fake: FakeSab, done: Path) -> Sabnzbd:
    return Sabnzbd(make_client("sabnzbd", fake), BASE, KEY, done)


async def test_a_queued_job_is_in_progress_and_the_users_jobs_are_never_read(
    make_client: Make, tmp_path: Path
) -> None:
    fake = FakeSab()
    client = sab(make_client, fake, tmp_path)
    info = await client.status(CAND)
    assert info.state is TransferStatus.IN_PROGRESS and info.bytes == 46_000_000
    (call,) = fake.calls  # the queue answered; history was not needed
    assert call["mode"] == "queue" and call["cat"] == CATEGORY and call["search"] == job_name(HASH)
    fake.drop("queue", OURS)
    fake.drop("history", OURS)  # only the user's same-named `tv` jobs are left
    assert (await client.status(CAND)).state is TransferStatus.FAILED
    await client.remove(HASH)
    assert fake.deletes() == []


async def test_a_completed_job_is_done_with_its_files_under_the_dir(
    make_client: Make, tmp_path: Path
) -> None:
    folder = tmp_path / "usenet" / job_name(HASH)
    (folder / "sub").mkdir(parents=True)
    (folder / "01 - Opening.flac").write_bytes(b"a")
    (folder / "sub" / "02 - Middle.flac").write_bytes(b"b")  # SABnzbd may unpack into a folder
    fake = FakeSab(folder)
    fake.drop("queue", OURS)
    info = await sab(make_client, fake, tmp_path / "usenet").status(CAND)
    assert info.state is TransferStatus.DONE and info.bytes == 91_800_000
    assert info.local_paths == (
        str(folder / "01 - Opening.flac"),
        str(folder / "sub" / "02 - Middle.flac"),
    )


async def test_a_job_finished_outside_the_dir_is_refused(make_client: Make, tmp_path: Path) -> None:
    fake = FakeSab(tmp_path / "elsewhere" / "x")
    fake.drop("queue", OURS)
    with pytest.raises(PermanentError, match="outside"):
        await sab(make_client, fake, tmp_path / "usenet").status(CAND)


@pytest.mark.parametrize("state", ["Failed", "Extracting"])
async def test_a_failed_job_fails_and_post_processing_is_in_progress(
    make_client: Make, tmp_path: Path, state: str
) -> None:
    fake = FakeSab()
    fake.drop("queue", OURS)
    fake.history["history"]["slots"][0]["status"] = state
    info = await sab(make_client, fake, tmp_path).status(CAND)
    want = TransferStatus.FAILED if state == "Failed" else TransferStatus.IN_PROGRESS
    assert info.state is want


async def test_add_sends_the_nzb_under_the_mixsync_category_once(
    make_client: Make, tmp_path: Path
) -> None:
    fake = FakeSab()
    client = sab(make_client, fake, tmp_path)
    await client.add(b"<nzb/>", HASH)  # already queued: a re-run adds nothing
    assert [c["mode"] for c in fake.calls] == ["queue"]
    fake.drop("queue", OURS)
    fake.drop("history", OURS)
    await client.add(b"<nzb/>", HASH)
    add = fake.calls[-1]
    assert add["mode"] == "addfile" and add["cat"] == CATEGORY
    assert add["nzbname"] == job_name(HASH) and b"<nzb/>" in fake.seen[-1].content


@pytest.mark.parametrize(
    ("where", "status", "want"),
    [
        ("queue", "", {"mode": "queue", "value": OURS, "del_files": "1"}),
        ("history", "Failed", {"mode": "history", "value": OURS, "archive": "0", "del_files": "1"}),
        ("history", "Completed", {"mode": "history", "value": OURS, "archive": "0"}),
    ],
)
async def test_remove_deletes_only_mixsyncs_job_and_files_only_if_it_never_completed(
    make_client: Make, tmp_path: Path, where: str, status: str, want: dict[str, str]
) -> None:
    fake = FakeSab()
    if where == "history":
        fake.drop("queue", OURS)
        fake.history["history"]["slots"][0]["status"] = status
    await sab(make_client, fake, tmp_path).remove(HASH)
    (call,) = fake.deletes()
    assert {k: v for k, v in call.items() if k not in ("apikey", "output", "name")} == want


async def test_the_api_key_is_never_in_a_url_or_a_log(
    make_client: Make, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeSab()
    client = sab(make_client, fake, tmp_path)
    await client.status(CAND)
    await client.remove(HASH)
    assert fake.seen and all(KEY not in str(r.url) for r in fake.seen)
    bad = Sabnzbd(make_client("sabnzbd", fake), BASE, "wrong", tmp_path)
    with pytest.raises(AuthError, match="MIXSYNC_SABNZBD_API_KEY"):
        await bad.status(CAND)
    assert KEY not in caplog.text


async def test_plain_text_errors_are_understood(make_client: Make, tmp_path: Path) -> None:
    def old(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="error: API Key Incorrect")

    with pytest.raises(AuthError):
        await Sabnzbd(make_client("sabnzbd", old), BASE, KEY, tmp_path).status(CAND)
