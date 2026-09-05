"""Pure-Python stand-in for the two rapidfuzz functions matcher.py uses.

Exists for exactly one reason: rapidfuzz is a C extension with no official
Pyodide/wasm wheel, so it cannot load inside the in-browser stlite build
(see web/README.md). matcher.py imports rapidfuzz normally everywhere else
— on the real server (Streamlit Cloud, local dev, tests) this module is
never touched, so precision/recall there are completely unaffected. It
only activates when `from rapidfuzz import fuzz` genuinely fails.

Algorithm: the same shape fuzzywuzzy (the library rapidfuzz itself
replaced) has used for years — difflib.SequenceMatcher under the hood.
Scores won't be bit-identical to rapidfuzz's C implementation, but track
it closely enough for the same confidence bands to fire; the in-browser
demo is a separate, explicitly-accepted deployment target, not the
evaluated/scored one.
"""
from __future__ import annotations

from difflib import SequenceMatcher


class _Fuzz:
    @staticmethod
    def ratio(a: str, b: str) -> float:
        if not a and not b:
            return 100.0
        return SequenceMatcher(None, a, b).ratio() * 100

    @staticmethod
    def partial_ratio(a: str, b: str) -> float:
        """Best-aligned substring ratio: slide the shorter string across
        candidate alignment windows in the longer one (found via the
        matcher's own matching blocks, not brute force) and keep the best
        score — this is exactly fuzzywuzzy's historical partial_ratio."""
        if not a or not b:
            return 0.0
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        blocks = SequenceMatcher(None, shorter, longer).get_matching_blocks()
        best = 0.0
        for block in blocks:
            start = max(block.b - block.a, 0)
            window = longer[start:start + len(shorter)]
            score = SequenceMatcher(None, shorter, window).ratio() * 100
            if score > 99.5:
                return 100.0
            best = max(best, score)
        return best

    @staticmethod
    def token_sort_ratio(a: str, b: str) -> float:
        def sorted_tokens(s: str) -> str:
            return " ".join(sorted(s.split()))
        return _Fuzz.ratio(sorted_tokens(a), sorted_tokens(b))


fuzz = _Fuzz()
