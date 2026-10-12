from datetime import UTC, datetime, timedelta

from mixsync.acquire.gate import DownloadGate

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def at(s: int) -> datetime:
    return T0 + timedelta(seconds=s)


def test_a_slow_transfer_stops_counting_and_counts_again_when_it_recovers() -> None:
    gate = DownloadGate(cap=2)
    other = 2_000_000  # a fast one; "a" at 1 MB/s is a third of the total, not a tenth
    for s in (0, 30):
        assert gate.counted(at(s), {"a": s * 1000, "b": s * other}) == 2  # window not yet full
    assert gate.counted(at(60), {"a": 60_000, "b": 60 * other}) == 1  # 1 KB/s < floor
    sent = 60_000
    for s in (90, 120, 150):  # speeds up to 1 MB/s
        sent += 30_000_000
        n = gate.counted(at(s), {"a": sent, "b": s * other})
    assert n == 2


def test_a_finished_transfer_forgets_its_samples() -> None:
    gate = DownloadGate()
    gate.counted(at(0), {"a": 0})
    gate.counted(at(60), {})
    assert gate.counted(at(120), {"a": 0}) == 1  # a fresh transfer: no history, counts
