from collections.abc import Sequence

from mixsync.core.matching import Band, Profile

# Starting points from docs/design/matching.md.
STRICT = Profile("strict", auto_accept_max=0.04, review_max=0.30)
BALANCED = Profile("balanced", auto_accept_max=0.08, review_max=0.40)
LOOSE = Profile("loose", auto_accept_max=0.15, review_max=0.55)


def band(distance: float, vetoes: Sequence[str], profile: Profile) -> Band:
    """Thresholds are inclusive. A veto caps the result at review; it never rescues a reject."""
    if distance > profile.review_max:
        return Band.REJECT
    if distance <= profile.auto_accept_max and not vetoes:
        return Band.AUTO_ACCEPT
    return Band.REVIEW
