"""Building a user-made playlist calls the sources it names, and every playlist builds."""

import pytest
from sqlalchemy import select

from mixsync.core.jobs import JobKind
from mixsync.core.types import RecordingRef
from mixsync.db.models.discovery import DiscoveryPlaylist

from .conftest import Env
from .test_discovery_playlists import Rig, ref


class RadioHistory:
    """Records what lb_radio and recommendations were asked for."""

    def __init__(self, rig: Rig) -> None:
        self.radio: list[tuple[str, str]] = []
        self.recs: list[int] = []
        rig.history.lb_radio = self._radio  # type: ignore[method-assign]
        rig.history.recommendations = self._recs  # type: ignore[method-assign]

    async def _radio(self, prompt: str, mode: str, token: str) -> list[RecordingRef]:
        self.radio.append((prompt, mode))
        return [ref(1)]

    async def _recs(self, user: str, token: str, count: int) -> list[RecordingRef]:
        self.recs.append(count)
        return [ref(2)]


@pytest.fixture
def rig(env: Env) -> Rig:
    return Rig(env)


async def test_every_playlist_builds_with_its_own_sources(rig: Rig) -> None:
    rig.link(rig.env.user_id)
    calls = RadioHistory(rig)
    with rig.env.sessions.begin() as s:
        for name, sources in (
            ("Radio", [{"type": "lb_radio", "prompt": "tag:(jazz)", "mode": "medium"}]),
            ("Recs", [{"type": "recommendations"}]),
        ):
            s.add(DiscoveryPlaylist(user_id=rig.env.user_id, name=name, sources=sources, size=10))
    rig.disc.start_chains()  # a build is queued for each, not just a default
    await rig.run(JobKind.DISCOVERY_BUILD)
    await rig.run(JobKind.DISCOVERY_BUILD)
    assert calls.radio == [("tag:(jazz)", "medium")]
    assert set(calls.recs) == {10}  # the Recs list, and the top-up of the short radio list
    with rig.env.sessions() as s:
        built = {p.name: p.entries for p in s.scalars(select(DiscoveryPlaylist))}
    assert [e["recording_mbid"][-1] for e in built["Radio"]] == ["1", "2"]  # radio, then top-up
    assert [e["recording_mbid"][-1] for e in built["Recs"]] == ["2"]
