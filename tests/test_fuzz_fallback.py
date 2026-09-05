"""Tests for the pure-Python rapidfuzz stand-in used only inside the
in-browser stlite build (web deploy) — see recon_agent/_fuzz_fallback.py
and matcher.py's try/except import.

Two things matter here: the fallback's own behavior is sane and matches
rapidfuzz closely on realistic reference-code text, AND matcher.py's real
import (which every other test in this suite exercises) is completely
unaffected — proven by reimporting matcher fresh with rapidfuzz hidden.
"""
import importlib
import sys

import pytest

from recon_agent._fuzz_fallback import fuzz as fallback_fuzz


def test_identical_strings_score_100():
    assert fallback_fuzz.ratio("INV20385", "INV20385") == 100.0
    assert fallback_fuzz.partial_ratio("INV20385", "INV20385") == 100.0


def test_partial_ratio_finds_exact_substring_regardless_of_surrounding_text():
    assert fallback_fuzz.partial_ratio("INV20385", "PAYOUT REF INV20385 SETTLED") == 100.0


def test_partial_ratio_of_empty_string_is_zero_not_a_crash():
    assert fallback_fuzz.partial_ratio("", "something") == 0.0
    assert fallback_fuzz.partial_ratio("something", "") == 0.0
    assert fallback_fuzz.partial_ratio("", "") == 0.0


def test_partial_ratio_of_unrelated_strings_is_low():
    assert fallback_fuzz.partial_ratio("XYZ999", "no relation at all") < 20


def test_token_sort_ratio_ignores_word_order():
    assert fallback_fuzz.token_sort_ratio("Acme Corp Ltd", "Ltd Corp Acme") == 100.0


def test_token_sort_ratio_penalizes_extra_words():
    score = fallback_fuzz.token_sort_ratio("John Doe", "Doe John Extra Words Here")
    assert 0 < score < 100


def test_scores_track_real_rapidfuzz_within_a_small_tolerance():
    """Not bit-identical (different algorithms), but close enough that the
    same confidence bands fire — see the module docstring for why exact
    parity isn't the goal."""
    real_fuzz = pytest.importorskip("rapidfuzz").fuzz

    cases = [
        ("INV20385", "PAYOUT REF INV20385 SETTLED"),
        ("ACME CORP", "settlement from ACME CORP LTD"),
        ("exact match here", "exact match here"),
        ("XYZ999", "no relation at all"),
        ("INV1234", "inv1235 typo case"),
    ]
    for a, b in cases:
        real = real_fuzz.partial_ratio(a, b)
        fake = fallback_fuzz.partial_ratio(a, b)
        assert abs(real - fake) <= 10, f"partial_ratio({a!r},{b!r}): real={real} fake={fake}"


def test_matcher_falls_back_to_pure_python_fuzz_when_rapidfuzz_is_unavailable(monkeypatch):
    """Simulates the exact condition the stlite/Pyodide build hits: the
    `rapidfuzz` C extension genuinely fails to import. matcher.py must
    still import cleanly and use the pure-Python fuzz module instead of
    raising ImportError at module load time."""
    monkeypatch.setitem(sys.modules, "rapidfuzz", None)  # None => import raises ImportError
    monkeypatch.delitem(sys.modules, "recon_agent.matcher", raising=False)
    monkeypatch.delitem(sys.modules, "recon_agent._fuzz_fallback", raising=False)

    matcher = importlib.import_module("recon_agent.matcher")
    try:
        assert matcher.fuzz.__class__.__module__ == "recon_agent._fuzz_fallback"
        # and it must actually work end-to-end, not just import
        assert matcher.fuzz.partial_ratio("ABC", "xxABCxx") == 100.0
    finally:
        # restore a clean import of matcher for every test after this one
        monkeypatch.undo()
        sys.modules.pop("recon_agent.matcher", None)
        importlib.import_module("recon_agent.matcher")


def test_matcher_uses_real_rapidfuzz_when_available():
    """The default, every-other-test-in-this-suite path: real rapidfuzz
    installed and imported normally, completely untouched by the fallback."""
    import recon_agent.matcher as matcher
    import rapidfuzz

    assert matcher.fuzz is rapidfuzz.fuzz
