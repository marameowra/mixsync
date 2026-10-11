"""Fingerprint submission to AcoustID after a promotion."""

import gzip
import logging
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest

from mixsync.acquire.acoustid_submit import POLL_DELAYS, AcoustIdSubmitter
from mixsync.core.config import Settings
from mixsync.core.errors import PermanentError, TransientError
from mixsync.core.jobs import JobKind
from mixsync.core.types import Fingerprint
from mixsync.metadata.acoustid import AcoustIdClient
from mixsync.ratelimit.client import PoliteClient

from .conftest import Env
from .fakes import RECORDINGS
from .test_recheck import BAD, Rig, _fingerprint, _rig

Make = Callable[[str, Callable[[httpx.Request], httpx.Response]], PoliteClient]
FP = Fingerprint(215.6, "AQADtEqUaEmUJAHAH")


class FakeSubmit:
    def __init__(self) -> None:
        self.batches: list[list[tuple[Fingerprint, str]]] = []
        self.polled: list[list[int]] = []
        self.statuses = ["pending"]  # the answers to successive polls; the last repeats

    async def submit(self, items: list[tuple[Fingerprint, str]]) -> list[int]:
        self.batches.append(items)
        return [10 + i for i in range(len(items))]

    async def submission_status(self, ids: list[int]) -> dict[int, str]:
        self.polled.append(ids)
        return dict.fromkeys(ids, self.statuses[min(len(self.polled), len(self.statuses)) - 1])


def _wire(env: Env, enabled: bool = True) -> tuple[FakeSubmit, AcoustIdSubmitter]:
    fake = FakeSubmit()
    sub = AcoustIdSubmitter(env.sessions, env.clock, env.ops, _fingerprint, fake, enabled)
    env.handlers = env.handlers | sub.handlers()
    return fake, sub


async def _promote(env: Env, rig: Rig) -> int:
    rig.seed()
    rig.schedule()
    await env.step()
    (plan,) = rig.plans()
    rig.merger.execute(plan.id)
    return plan.id


async def test_promote_submits_one_batch_then_polls(env: Env) -> None:
    rig = _rig(env)
    fake, _ = _wire(env)
    pid = await _promote(env, rig)
    (job,) = env.jobs(JobKind.ACOUSTID_SUBMIT)
    assert job.payload == {"plan_id": pid}
    await env.step()  # the submission
    assert [[(f.fingerprint, m) for f, m in b] for b in fake.batches] == [
        [("Song One", RECORDINGS["Song One"]), ("Song Two", RECORDINGS["Song Two"])]
    ]
    (poll,) = env.jobs(JobKind.ACOUSTID_STATUS)
    assert poll.payload == {"plan_id": pid, "ids": [10, 11], "attempt": 0}
    assert poll.run_after.replace(tzinfo=env.clock.now().tzinfo) == (
        env.clock.now() + POLL_DELAYS[0]
    )


async def test_polling_stops_after_three_tries(env: Env) -> None:
    rig = _rig(env)
    fake, _ = _wire(env)
    await _promote(env, rig)
    await env.drain()
    assert len(fake.polled) == 3
    assert [j.payload["attempt"] for j in env.jobs(JobKind.ACOUSTID_STATUS)] == [0, 1, 2]


async def test_polling_stops_at_a_final_status(env: Env) -> None:
    rig = _rig(env)
    fake, _ = _wire(env)
    fake.statuses = ["pending", "imported"]
    await _promote(env, rig)
    await env.drain()
    assert len(fake.polled) == 2


async def test_without_keys_nothing_is_sent(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    rig = _rig(env)
    fake, _ = _wire(env, enabled=False)
    with caplog.at_level(logging.INFO):
        await _promote(env, rig)
        await env.drain()
    assert fake.batches == [] and env.jobs(JobKind.ACOUSTID_STATUS) == []
    assert sum("keys are not set" in r.message for r in caplog.records) == 1


async def test_unfingerprintable_file_is_skipped(env: Env) -> None:
    rig = _rig(env)
    fake, _ = _wire(env)
    await _promote(env, rig)
    BAD.add("Song One")
    await env.step()
    assert [[m for _, m in b] for b in fake.batches] == [[RECORDINGS["Song Two"]]]


async def test_undo_and_a_retry_enqueue_nothing_more(env: Env) -> None:
    rig = _rig(env)
    fake, sub = _wire(env)
    pid = await _promote(env, rig)
    rig.merger.execute(pid)  # already done: no second job
    rig.merger.undo_plan(pid)
    assert len(env.jobs(JobKind.ACOUSTID_SUBMIT)) == 1
    await env.step()
    sub._enqueue_poll(pid, [10, 11], 0)  # a retried submit re-enqueues the same status job
    assert len(fake.batches) == 1 and len(env.jobs(JobKind.ACOUSTID_STATUS)) == 1


def _client(settings: Settings, handler: Callable[[httpx.Request], httpx.Response], make: Make):
    s = settings.model_copy(update={"acoustid_app_key": "app", "acoustid_user_key": "usr"})
    return AcoustIdClient(make("acoustid", handler), s)


async def test_submit_request_shape(make_client: Make, settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"status": "ok", "submissions": [{"id": 5, "index": 0}, {"id": 6, "index": 1}]}
        return httpx.Response(200, json=body)

    items = [(FP, "mbid-a"), (Fingerprint(100.4, "AQ2"), "mbid-b")]
    assert await _client(settings, handler, make_client).submit(items) == [5, 6]
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/v2/submit"
    assert req.headers["content-encoding"] == "gzip"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(gzip.decompress(req.content).decode()).items()}
    assert form == {
        "client": "app",
        "user": "usr",
        "format": "json",
        "fingerprint.0": FP.fingerprint,
        "duration.0": "216",
        "mbid.0": "mbid-a",
        "fingerprint.1": "AQ2",
        "duration.1": "100",
        "mbid.1": "mbid-b",
    }


async def test_submit_errors(make_client: Make, settings: Settings) -> None:
    bad = _client(settings, lambda _r: httpx.Response(200, json={"status": "error"}), make_client)
    with pytest.raises(PermanentError):
        await bad.submit([(FP, "m")])
    down = _client(settings, lambda _r: httpx.Response(500), make_client)
    with pytest.raises(TransientError):
        await down.submit([(FP, "m")])
    bare = AcoustIdClient(make_client("acoustid", lambda _r: httpx.Response(200)), settings)
    with pytest.raises(RuntimeError, match="USER_KEY"):
        await bare.submit([(FP, "m")])


async def test_submission_status_repeats_id_and_skips_the_cache(
    make_client: Make, settings: Settings
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"status": "ok", "submissions": [{"id": 5, "status": "pending"}]}
        return httpx.Response(200, json=body)

    client = _client(settings, handler, make_client)
    assert await client.submission_status([5, 6]) == {5: "pending"}
    await client.submission_status([5, 6])
    assert len(seen) == 2  # not cached
    assert seen[0].url.params.get_list("id") == ["5", "6"]
