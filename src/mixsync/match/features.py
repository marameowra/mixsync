# Ported from beets (https://github.com/beetbox/beets) at commit
# b8c9661fe10463cc761b83f8efd861cb49224224, beets/autotag/distance.py.
#
# The MIT License
#
# Copyright (c) 2010-2016 Adrian Sampson
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""String similarity, ported from beets' `string_dist`."""

import re
import unicodedata

from rapidfuzz.distance import Levenshtein

# Words that can be moved to the end of a string using a comma.
SD_END_WORDS = ["the", "a", "an"]
# Reduced weights for certain portions of the string.
SD_PATTERNS = [
    (r"^the ", 0.1),
    (r"[\[\(]?(ep|single)[\]\)]?", 0.0),
    (r"[\[\(]?\b(featuring|feat|ft)\b[\. :].+", 0.1),
    (r"\(.*?\)", 0.3),
    (r"\[.*?\]", 0.3),
    (r"(, )?(pt\.|part) .+", 0.2),
]
# Replacements to use before testing distance.
SD_REPLACE = [(r"&", "and")]


def _normalize(s: str) -> str:
    # Unlike beets (unidecode + [a-z0-9]) this keeps non-Latin letters, so two different
    # Cyrillic or CJK strings do not both collapse to "" and compare equal.
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if c.isalnum() and not unicodedata.combining(c))


def _string_dist_basic(str1: str, str2: str) -> float:
    """Edit distance ignoring case, accents and punctuation, normalized by length."""
    str1, str2 = _normalize(str1), _normalize(str2)
    if not str1 and not str2:
        return 0.0
    return Levenshtein.distance(str1, str2) / max(len(str1), len(str2))


def string_dist(str1: str | None, str2: str | None) -> float:
    """An "intuitive" edit distance in [0, 1], with tweaks that reflect intuition about text."""
    if str1 is None and str2 is None:
        return 0.0
    if str1 is None or str2 is None:
        return 1.0

    str1 = str1.casefold()
    str2 = str2.casefold()

    # "the something" equals "something, the".
    for word in SD_END_WORDS:
        if str1.endswith(f", {word}"):
            str1 = f"{word} {str1[: -len(word) - 2]}"
        if str2.endswith(f", {word}"):
            str2 = f"{word} {str2[: -len(word) - 2]}"

    for pat, repl in SD_REPLACE:
        str1 = re.sub(pat, repl, str1)
        str2 = re.sub(pat, repl, str2)

    # Drop each pattern in turn and charge only a fraction of the distance it removed.
    base_dist = _string_dist_basic(str1, str2)
    penalty = 0.0
    for pat, weight in SD_PATTERNS:
        case_str1 = re.sub(pat, "", str1)
        case_str2 = re.sub(pat, "", str2)
        if case_str1 != str1 or case_str2 != str2:
            case_dist = _string_dist_basic(case_str1, case_str2)
            case_delta = max(0.0, base_dist - case_dist)
            if case_delta == 0.0:
                continue
            str1 = case_str1
            str2 = case_str2
            base_dist = case_dist
            penalty += weight * case_delta

    return base_dist + penalty
