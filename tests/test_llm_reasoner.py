"""Tier-1 Exception Investigator: the guardrail (never crashes, always
degrades to the rule-based fallback with no key or a failed call) and the
honest source labelling that the whole audit story depends on.

Provider: Google Gemini via google-genai. Fakes are built to match the real
SDK shape verified against the live API (see llm_reasoner.py's comments):
`genai.Client(api_key=...).models.generate_content(model=, contents=,
config=)` returning an object with `.text` and `.usage_metadata`.
"""
import sys
import types as _pytypes

import pytest

from recon_agent.llm_reasoner import InvestigatorResult, _rule_based_fallback, investigate


def _install_fake_genai(monkeypatch, generate_content):
    """Installs fake `google`, `google.genai`, and `google.genai.types`
    modules so `from google import genai` / `from google.genai import types`
    resolve to test doubles instead of the real SDK. `generate_content` is
    the fake `client.models.generate_content(...)` implementation."""
    fake_types = _pytypes.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs  # never inspected by the fakes below

    class _FakeModels:
        @staticmethod
        def generate_content(*, model, contents, config):
            return generate_content(model=model, contents=contents, config=config)

    class _FakeClient:
        def __init__(self, *a, **k):
            self.models = _FakeModels()

    fake_genai = _pytypes.ModuleType("google.genai")
    fake_genai.Client = _FakeClient
    fake_genai.types = fake_types

    fake_google = _pytypes.ModuleType("google")
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)


class _FakeUsage:
    def __init__(self, prompt=0, candidates=0, thoughts=0):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts


class _FakeResponse:
    def __init__(self, text, prompt=0, candidates=0, thoughts=0):
        self.text = text
        self.usage_metadata = _FakeUsage(prompt, candidates, thoughts)


def test_no_candidates_returns_none_not_a_crash():
    result = _rule_based_fallback({"txn_id": "T1", "amount": 100}, [])
    assert result.chosen_invoice_id is None
    assert result.source == "rule_based_fallback"


def test_high_score_candidate_is_chosen():
    candidates = [{"invoice_id": "INV1", "ref_score": 90, "amount_score": 90,
                  "date_score": 90, "composite_score": 90}]
    result = _rule_based_fallback({"txn_id": "T1"}, candidates)
    assert result.chosen_invoice_id == "INV1"
    assert result.confidence <= 89  # capped below the LLM-confirmed range


def test_low_score_candidate_declines_to_guess():
    candidates = [{"invoice_id": "INV1", "ref_score": 20, "amount_score": 10,
                  "date_score": 10, "composite_score": 40}]
    result = _rule_based_fallback({"txn_id": "T1"}, candidates)
    assert result.chosen_invoice_id is None


def test_investigate_falls_back_with_no_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=True)
    assert result.source == "rule_based_fallback"


def test_investigate_forced_rule_based_even_with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=False)
    assert result.source == "rule_based_fallback"


def test_investigate_degrades_gracefully_on_api_failure(monkeypatch):
    """A live call that raises must still return a usable result, never
    propagate the exception up into the reconciliation pipeline."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    def boom(**kwargs):
        raise RuntimeError("simulated network failure")

    _install_fake_genai(monkeypatch, boom)

    result = investigate({"txn_id": "T1", "narration": "x", "amount": 100},
                         [{"invoice_id": "INV1", "composite_score": 80, "ref_score": 80,
                          "amount_score": 80, "date_score": 80}],
                         use_llm=True)
    assert result.source == "rule_based_fallback"
    assert "LLM call failed" in result.rationale
    assert result.chosen_invoice_id == "INV1"  # the fallback still did its job


def test_investigate_records_token_usage_on_live_call(monkeypatch):
    """Regression test for the token-tracking plumbing added for the cost
    strip -- a live call's usage must reach the InvestigatorResult, and
    thinking tokens must be folded into output_tokens (Gemini bills them at
    the output rate)."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    def fake_call(**kwargs):
        return _FakeResponse('{"chosen_invoice_id": "INV1", "confidence": 90, "rationale": "test"}',
                             prompt=123, candidates=30, thoughts=15)

    _install_fake_genai(monkeypatch, fake_call)

    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=True)
    assert result.source == "llm"
    assert result.input_tokens == 123
    assert result.output_tokens == 45  # candidates (30) + thoughts (15)


def test_rule_based_fallback_proposes_a_batch_when_amounts_reconcile():
    """Item 5, tightened: a batch is only proposed when the qualifying
    candidates' combined remaining_amount actually reconciles against the
    settlement -- not just because more than one candidate scored decently."""
    candidates = [
        {"invoice_id": "INV1", "ref_score": 85, "amount_score": 70, "date_score": 70,
         "composite_score": 80, "remaining_amount": 3000.0},
        {"invoice_id": "INV2", "ref_score": 70, "amount_score": 65, "date_score": 65,
         "composite_score": 68, "remaining_amount": 4000.0},
        {"invoice_id": "INV3", "ref_score": 20, "amount_score": 10, "date_score": 10,
         "composite_score": 30, "remaining_amount": 500.0},
    ]
    result = _rule_based_fallback({"txn_id": "T1", "amount": 7000.0}, candidates)
    assert set(result.chosen_invoice_ids) == {"INV1", "INV2"}
    assert "INV3" not in result.chosen_invoice_ids  # below threshold regardless
    assert result.confidence == 68  # min of the qualifying set, not the max
    assert "batch proposal" in result.rationale.lower()


def test_rule_based_fallback_does_not_guess_a_batch_without_amount_corroboration():
    """Two candidates both score above the bar, but their combined balance
    does NOT reconcile against the settlement -- must propose only the
    single best one, not a guessed batch (this is the fix for the 5.2pp
    precision/recall regression an earlier, looser version introduced)."""
    candidates = [
        {"invoice_id": "INV1", "ref_score": 85, "amount_score": 70, "date_score": 70,
         "composite_score": 80, "remaining_amount": 3000.0},
        {"invoice_id": "INV2", "ref_score": 70, "amount_score": 65, "date_score": 65,
         "composite_score": 68, "remaining_amount": 4000.0},
    ]
    result = _rule_based_fallback({"txn_id": "T1", "amount": 3000.0}, candidates)
    assert result.chosen_invoice_ids == ["INV1"]  # single best, not a batch guess


def test_single_qualifying_candidate_still_reads_as_a_single_proposal():
    candidates = [{"invoice_id": "INV1", "ref_score": 85, "amount_score": 70,
                  "date_score": 70, "composite_score": 80}]
    result = _rule_based_fallback({"txn_id": "T1"}, candidates)
    assert result.chosen_invoice_ids == ["INV1"]
    assert result.chosen_invoice_id == "INV1"  # backward-compatible accessor


def test_live_llm_can_return_a_batch(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    def fake_call(**kwargs):
        return _FakeResponse(
            '{"chosen_invoice_ids": ["INV1", "INV2"], "confidence": 88, "rationale": "batched payout"}',
            prompt=200, candidates=20, thoughts=10)

    _install_fake_genai(monkeypatch, fake_call)

    candidates = [{"invoice_id": "INV1", "composite_score": 80, "ref_score": 80,
                  "amount_score": 80, "date_score": 80},
                 {"invoice_id": "INV2", "composite_score": 75, "ref_score": 75,
                  "amount_score": 75, "date_score": 75}]
    result = investigate({"txn_id": "T1", "narration": "x"}, candidates, use_llm=True)
    assert result.source == "llm"
    assert result.chosen_invoice_ids == ["INV1", "INV2"]


def test_live_llm_response_cannot_propose_an_unoffered_invoice_id(monkeypatch):
    """A model hallucinating an invoice_id outside the candidate list it was
    given must never reach the pipeline as a proposal."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    def fake_call(**kwargs):
        return _FakeResponse(
            '{"chosen_invoice_ids": ["INV1", "INV_NOT_OFFERED"], "confidence": 90, "rationale": "x"}',
            prompt=10, candidates=10)

    _install_fake_genai(monkeypatch, fake_call)

    candidates = [{"invoice_id": "INV1", "composite_score": 80, "ref_score": 80,
                  "amount_score": 80, "date_score": 80}]
    result = investigate({"txn_id": "T1", "narration": "x"}, candidates, use_llm=True)
    assert result.chosen_invoice_ids == ["INV1"]
    assert "INV_NOT_OFFERED" not in result.chosen_invoice_ids


def test_no_llm_path_labels_correctly(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=True)
    assert result.llm_path == "no_llm"
    assert result.attempts == 0


def test_transient_error_retries_then_succeeds(monkeypatch):
    """A rate-limit-shaped error (HTTP 429 via `.code`) on the first
    attempt, success on the second -- must retry, call on_retry exactly
    once, and label the path correctly."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    class RateLimitError(Exception):
        code = 429

    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RateLimitError("slow down")
        return _FakeResponse('{"chosen_invoice_ids": ["INV1"], "confidence": 90, "rationale": "ok"}',
                             prompt=50, candidates=10)

    _install_fake_genai(monkeypatch, fake_call)

    retry_calls = []
    result = investigate(
        {"txn_id": "T1", "narration": "x"},
        [{"invoice_id": "INV1", "composite_score": 80, "ref_score": 80, "amount_score": 80, "date_score": 80}],
        use_llm=True,
        on_retry=lambda attempt, max_attempts, txn_id: retry_calls.append((attempt, max_attempts, txn_id)),
    )
    assert call_count["n"] == 2
    assert result.source == "llm"
    assert result.llm_path == "retried_then_succeeded"
    assert result.attempts == 2
    assert retry_calls == [(2, 3, "T1")]


def test_transient_error_exhausts_retries_then_falls_back(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    class ServerError(Exception):
        code = 503

    def fake_call(**kwargs):
        raise ServerError("too slow")

    _install_fake_genai(monkeypatch, fake_call)

    retry_calls = []
    result = investigate(
        {"txn_id": "T1", "narration": "x"},
        [{"invoice_id": "INV1", "composite_score": 80, "ref_score": 80, "amount_score": 80, "date_score": 80}],
        use_llm=True,
        on_retry=lambda a, m, t: retry_calls.append(a),
    )
    assert result.source == "rule_based_fallback"
    assert result.llm_path == "retried_then_fell_back"
    assert result.attempts == 3
    assert retry_calls == [2, 3]  # called before attempt 2 and attempt 3
    assert "3 attempt(s)" in result.rationale


def test_non_transient_error_does_not_retry(monkeypatch):
    """A genuinely non-transient failure (e.g. bad JSON) must fall back
    immediately, without burning through retries or calling on_retry."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    call_count = {"n": 0}

    def fake_call(**kwargs):
        call_count["n"] += 1
        return _FakeResponse("not valid json at all", prompt=5, candidates=5)

    _install_fake_genai(monkeypatch, fake_call)

    retry_calls = []
    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=True,
                         on_retry=lambda a, m, t: retry_calls.append(a))
    assert call_count["n"] == 1  # no retry attempted
    assert result.llm_path == "failed_first_try_fell_back"
    assert result.attempts == 1
    assert retry_calls == []


def test_succeeded_first_try_path_label(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    def fake_call(**kwargs):
        return _FakeResponse('{"chosen_invoice_ids": [], "confidence": 10, "rationale": "no match"}',
                             prompt=5, candidates=5)

    _install_fake_genai(monkeypatch, fake_call)

    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=True)
    assert result.llm_path == "succeeded_first_try"
    assert result.attempts == 1


def test_google_api_key_env_var_also_works(monkeypatch):
    """GEMINI_API_KEY is preferred, but GOOGLE_API_KEY (the SDK's own native
    fallback name) must also be honored."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key-for-test")

    def fake_call(**kwargs):
        return _FakeResponse('{"chosen_invoice_ids": [], "confidence": 5, "rationale": "no match"}',
                             prompt=5, candidates=5)

    _install_fake_genai(monkeypatch, fake_call)

    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=True)
    assert result.source == "llm"
