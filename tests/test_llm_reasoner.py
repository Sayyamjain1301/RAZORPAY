"""Tier-1 Exception Investigator: the guardrail (never crashes, always
degrades to the rule-based fallback with no key or a failed call) and the
honest source labelling that the whole audit story depends on."""
import os

import pytest

from recon_agent.llm_reasoner import InvestigatorResult, _rule_based_fallback, investigate


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
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=True)
    assert result.source == "rule_based_fallback"


def test_investigate_forced_rule_based_even_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-for-test")
    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=False)
    assert result.source == "rule_based_fallback"


def test_investigate_degrades_gracefully_on_api_failure(monkeypatch):
    """A live call that raises must still return a usable result, never
    propagate the exception up into the reconciliation pipeline."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-for-test")

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(*a, **k):
                raise RuntimeError("simulated network failure")

    import sys
    import types
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = _BoomClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    result = investigate({"txn_id": "T1", "narration": "x", "amount": 100},
                         [{"invoice_id": "INV1", "composite_score": 80, "ref_score": 80,
                          "amount_score": 80, "date_score": 80}],
                         use_llm=True)
    assert result.source == "rule_based_fallback"
    assert "LLM call failed" in result.rationale
    assert result.chosen_invoice_id == "INV1"  # the fallback still did its job


def test_investigate_records_token_usage_on_live_call(monkeypatch):
    """Regression test for the token-tracking plumbing added for the cost
    strip — a live call's usage must reach the InvestigatorResult."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-for-test")

    class _FakeUsage:
        input_tokens = 123
        output_tokens = 45

    class _FakeBlock:
        text = '{"chosen_invoice_id": "INV1", "confidence": 90, "rationale": "test"}'

    class _FakeMessage:
        content = [_FakeBlock()]
        usage = _FakeUsage()

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(*a, **k):
                return _FakeMessage()

    import sys
    import types
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    result = investigate({"txn_id": "T1", "narration": "x"}, [], use_llm=True)
    assert result.source == "llm"
    assert result.input_tokens == 123
    assert result.output_tokens == 45


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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-for-test")

    class _FakeBlock:
        text = '{"chosen_invoice_ids": ["INV1", "INV2"], "confidence": 88, "rationale": "batched payout"}'

    class _FakeUsage:
        input_tokens = 200
        output_tokens = 30

    class _FakeMessage:
        content = [_FakeBlock()]
        usage = _FakeUsage()

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(*a, **k):
                return _FakeMessage()

    import sys
    import types
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-for-test")

    class _FakeBlock:
        text = '{"chosen_invoice_ids": ["INV1", "INV_NOT_OFFERED"], "confidence": 90, "rationale": "x"}'

    class _FakeUsage:
        input_tokens = 10
        output_tokens = 10

    class _FakeMessage:
        content = [_FakeBlock()]
        usage = _FakeUsage()

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        class messages:
            @staticmethod
            def create(*a, **k):
                return _FakeMessage()

    import sys
    import types
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    candidates = [{"invoice_id": "INV1", "composite_score": 80, "ref_score": 80,
                  "amount_score": 80, "date_score": 80}]
    result = investigate({"txn_id": "T1", "narration": "x"}, candidates, use_llm=True)
    assert result.chosen_invoice_ids == ["INV1"]
    assert "INV_NOT_OFFERED" not in result.chosen_invoice_ids
