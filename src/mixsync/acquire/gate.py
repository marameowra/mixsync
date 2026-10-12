"""The cap on concurrent Soulseek downloads, with an exemption for transfers too slow to matter."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class DownloadGate:
    cap: int = 3
    window: timedelta = timedelta(seconds=60)
    floor: float = 50_000  # bytes/s
    share: float = 0.10  # of the total throughput of all moving transfers
    # In-process, not in the DB: only the worker starts downloads, and a restart merely makes
    # every transfer count again until a full window is seen. shortcut: two worker processes
    # would each see only their own samples; upgrade to a table if workers are ever scaled out.
    _samples: dict[str, list[tuple[datetime, int]]] = field(
        default_factory=dict[str, list[tuple[datetime, int]]]
    )

    def counted(self, now: datetime, active: dict[str, int]) -> int:
        """Sample the moving transfers (id -> bytes so far) and count those that fill a slot."""
        for gone in set(self._samples) - set(active):
            del self._samples[gone]
        rates: dict[str, float] = {}
        for tid, sent in active.items():
            hist = self._samples.setdefault(tid, [])
            hist.append((now, sent))
            # keep one baseline at or before the window start, so the average spans the window
            while len(hist) > 1 and hist[1][0] <= now - self.window:
                hist.pop(0)
            t0, b0 = hist[0]
            if t0 <= now - self.window:
                rates[tid] = (sent - b0) / (now - t0).total_seconds()
        total = sum(rates.values())
        slow = sum(1 for r in rates.values() if r < self.floor or r < self.share * total)
        return len(active) - slow

    def full(self, now: datetime, active: dict[str, int]) -> bool:
        return self.counted(now, active) >= self.cap
