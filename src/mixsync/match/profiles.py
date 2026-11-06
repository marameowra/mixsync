from collections.abc import Sequence

from mixsync.core.matching import Band, Profile

# Starting points from docs/design/matching.md.
STRICT = Profile("strict", auto_accept_max=0.04, review_max=0.30)
BALANCED = Profile("balanced", auto_accept_max=0.08, review_max=0.40)
LOOSE = Profile("loose", auto_accept_max=0.15, review_max=0.55)
# Fixed, not the profile's limit: a user's auto-accept setting must not move the coin-flip window.
RUNNER_UP_GAP = 0.08


def confidence(distance: float) -> int:
    """How the UI shows a distance limit: 0.08 is 92%."""
    return round((1 - distance) * 100)


def from_confidence(percent: int) -> float:
    return (100 - percent) / 100


def band(distance: float, vetoes: Sequence[str], profile: Profile) -> Band:
    """Thresholds are inclusive. Any veto forces review, whatever the distance."""
    if vetoes:
        return Band.REVIEW
    if distance > profile.review_max:
        return Band.REJECT
    if distance <= profile.auto_accept_max and not vetoes:
        return Band.AUTO_ACCEPT
    return Band.REVIEW


def close_gap(gap: float) -> bool:
    """A runner-up this near the best is a coin flip. Inclusive, with float slack."""
    return gap <= RUNNER_UP_GAP + 1e-9
