"""Tests for the value-weighted Overview analytics.

These are arithmetic over real money figures shown to a finance user, so
the expected values here are computed by hand in the test rather than
asserted loosely.
"""
from datetime import date

from recon_agent.insights import (aging, concentration, deduction_leakage,
                                  top_exposure, value_summary)


def _status_of(s):
    return s["status"]


def _settlement(txn_id, status, layer="exact_reference+deduction_engine",
                deduction_label=None, matched=(), confidence=100):
    return {"txn_id": txn_id, "status": status, "layer": layer,
            "deduction_label": deduction_label, "matched_invoice_ids": list(matched),
            "confidence": confidence}


def test_value_summary_splits_rupees_and_rows_separately():
    settlements = [
        _settlement("T1", "matched"),
        _settlement("T2", "matched"),
        _settlement("T3", "exception"),
    ]
    # 2 of 3 rows matched (66.7%), but only 200 of 1200 rupees (16.7%) --
    # the exact divergence a count-only dashboard hides.
    enriched = {"T1": {"amount": 100.0}, "T2": {"amount": 100.0}, "T3": {"amount": 1000.0}}

    out = value_summary(settlements, enriched, _status_of)

    assert out["total_value"] == 1200.0
    assert out["matched_value"] == 200.0
    assert out["exception_value"] == 1000.0
    assert out["matched_count"] == 2
    assert round(out["matched_count_pct"], 4) == round(2 / 3, 4)
    assert round(out["matched_value_pct"], 4) == round(200 / 1200, 4)
    # value trails count by ~50pp: the expensive item is the stuck one
    assert out["value_vs_count_pp"] < -49
    assert out["at_risk_value"] == 1000.0


def test_value_summary_counts_pending_as_exposure_not_reconciled():
    settlements = [_settlement("T1", "matched"), _settlement("T2", "pending_confirmation")]
    enriched = {"T1": {"amount": 500.0}, "T2": {"amount": 300.0}}

    out = value_summary(settlements, enriched, _status_of)

    assert out["matched_value"] == 500.0
    assert out["pending_value"] == 300.0
    # pending is still unreconciled money until a human clicks confirm
    assert out["at_risk_value"] == 300.0


def test_value_summary_respects_the_status_function_passed_in():
    """app.py passes effective_status(), which folds in this session's
    confirms -- a confirmed row must move into the matched bucket."""
    settlements = [_settlement("T1", "pending_confirmation")]
    enriched = {"T1": {"amount": 750.0}}

    out = value_summary(settlements, enriched, lambda s: "matched")

    assert out["matched_value"] == 750.0
    assert out["at_risk_value"] == 0.0


def test_value_summary_handles_empty_batch_without_dividing_by_zero():
    out = value_summary([], {}, _status_of)
    assert out["total_value"] == 0
    assert out["matched_value_pct"] == 0.0
    assert out["value_vs_count_pp"] == 0.0


def test_deduction_leakage_computes_fee_gst_and_tds_in_rupees():
    invoices = [{"invoice_id": "INV1", "amount": 10000.0},
                {"invoice_id": "INV2", "amount": 50000.0}]
    settlements = [
        _settlement("T1", "matched", deduction_label="gateway_fee(2.0%)+gst(18.0%_on_fee)",
                    matched=["INV1"]),
        _settlement("T2", "matched", deduction_label="tds(2%)", matched=["INV2"]),
    ]

    out = deduction_leakage(settlements, invoices, {}, _status_of)

    assert out["gateway_fees"] == 200.0            # 2% of 10000
    assert out["gst_on_fees"] == 36.0              # 18% of the 200 fee
    assert out["tds"] == 1000.0                    # 2% of 50000
    assert out["gross"] == 60000.0
    assert out["total_deducted"] == 1236.0
    assert out["net_received"] == 58764.0
    assert out["n_settlements"] == 2


def test_deduction_leakage_excludes_partial_payments():
    """A partial payment's invoice `amount` is the full original total, not
    what this settlement covered -- including it would overstate the gross
    and invent a deduction that never happened."""
    invoices = [{"invoice_id": "INV1", "amount": 10000.0}]
    settlements = [_settlement("T1", "matched", layer="exact_reference+partial_payment",
                               deduction_label="gateway_fee(2.0%)+gst(18.0%_on_fee)",
                               matched=["INV1"])]

    out = deduction_leakage(settlements, invoices, {}, _status_of)

    assert out["gross"] == 0.0
    assert out["total_deducted"] == 0.0
    assert out["n_settlements"] == 0


def test_deduction_leakage_ignores_unmatched_and_undeducted_rows():
    invoices = [{"invoice_id": "INV1", "amount": 1000.0}]
    settlements = [
        _settlement("T1", "exception", deduction_label="tds(2%)", matched=["INV1"]),
        _settlement("T2", "matched", deduction_label="none", matched=["INV1"]),
        _settlement("T3", "matched", deduction_label=None, matched=["INV1"]),
    ]

    out = deduction_leakage(settlements, invoices, {}, _status_of)

    assert out["total_deducted"] == 0.0
    assert out["n_settlements"] == 0


def test_deduction_leakage_effective_rate_is_share_of_gross():
    invoices = [{"invoice_id": "INV1", "amount": 1000.0}]
    settlements = [_settlement("T1", "matched", deduction_label="tds(10%)", matched=["INV1"])]

    out = deduction_leakage(settlements, invoices, {}, _status_of)

    assert out["tds"] == 100.0
    assert round(out["effective_rate"], 4) == 0.1


def test_aging_buckets_by_days_since_settlement_date():
    today = date(2026, 9, 5)
    unresolved = [
        _settlement("T1", "exception"),   # 2 days
        _settlement("T2", "exception"),   # 10 days
        _settlement("T3", "exception"),   # 40 days
    ]
    enriched = {
        "T1": {"amount": 100.0, "txn_date": "2026-09-03"},
        "T2": {"amount": 200.0, "txn_date": "2026-08-26"},
        "T3": {"amount": 300.0, "txn_date": "2026-07-27"},
    }

    out = aging(unresolved, enriched, today=today)

    assert out["buckets"]["0-7 days"]["count"] == 1
    assert out["buckets"]["0-7 days"]["value"] == 100.0
    assert out["buckets"]["8-14 days"]["count"] == 1
    assert out["buckets"]["30+ days"]["count"] == 1
    assert out["buckets"]["30+ days"]["value"] == 300.0
    assert out["oldest_days"] == 40
    assert out["total_value"] == 600.0


def test_aging_handles_missing_or_malformed_dates_without_crashing():
    unresolved = [_settlement("T1", "exception"), _settlement("T2", "exception")]
    enriched = {"T1": {"amount": 50.0, "txn_date": None},
                "T2": {"amount": 70.0, "txn_date": "not-a-date"}}

    out = aging(unresolved, enriched, today=date(2026, 9, 5))

    assert out["undated"]["count"] == 2
    assert out["undated"]["value"] == 120.0
    assert out["total_count"] == 2


def test_top_exposure_sorts_by_amount_descending_and_limits():
    unresolved = [_settlement(f"T{i}", "exception") for i in range(1, 5)]
    enriched = {"T1": {"amount": 10.0}, "T2": {"amount": 900.0},
                "T3": {"amount": 50.0}, "T4": {"amount": 400.0}}

    out = top_exposure(unresolved, enriched, limit=2)

    assert [r["txn_id"] for r in out] == ["T2", "T4"]
    assert out[0]["amount"] == 900.0


def test_concentration_is_top_n_share_of_total_exposure():
    unresolved = [_settlement(f"T{i}", "exception") for i in range(1, 4)]
    enriched = {"T1": {"amount": 800.0}, "T2": {"amount": 100.0}, "T3": {"amount": 100.0}}

    # top 1 of 1000 total = 80%
    assert round(concentration(unresolved, enriched, top_n=1), 4) == 0.8


def test_concentration_of_nothing_is_zero_not_a_crash():
    assert concentration([], {}, top_n=5) == 0.0
