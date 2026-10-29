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
    fast = 2_000_000
    gate.counted(at(0), {"a": 0, "b": 0})
    gate.counted(at(60), {"b": 60 * fast})  # "a" finished
    # "a" is back with no progress: a fresh transfer with no history counts; remembered
    # samples would show it stalled next to "b" and exempt it
    assert gate.counted(at(120), {"a": 0, "b": 120 * fast}) == 2


def run(gate: DownloadGate, rates: list[int]) -> int:
    """Feed constant per-transfer rates (bytes/s) over two windows; the last count."""
    n = 0
    for s in (0, 30, 60, 90, 120):
        n = gate.counted(at(s), {str(i): r * s for i, r in enumerate(rates)})
    return n


def test_equal_rates_never_exempt_each_other() -> None:
    assert run(DownloadGate(cap=3), [1_000_000] * 11) == 11  # each is 9% of the total


def test_a_saturated_link_does_not_exempt_everything() -> None:
    assert run(DownloadGate(cap=3), [40_000] * 3) == 3  # 120 KB/s total < 3 x 50 KB/s
