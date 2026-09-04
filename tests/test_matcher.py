"""Core reconciliation pipeline: exact-reference matching, batch narrations,
partial payments, and the closing-arithmetic invariant (matched + pending +
exception == total settlements) that the whole scorecard depends on."""
from recon_agent.matcher import _normalize, compute_metrics, reconcile


def _inv(invoice_id, customer, invoice_date, amount, ref):
    return {"invoice_id": invoice_id, "customer": customer, "invoice_date": invoice_date,
           "amount": amount, "remaining_amount": amount, "reference_code": ref, "status": "open"}


def _settle(txn_id, txn_date, amount, narration):
    return {"txn_id": txn_id, "txn_date": txn_date, "amount": amount, "narration": narration}


def test_normalize_strips_non_alnum_and_uppercases():
    assert _normalize("rjpy-2001") == "RJPY2001"
    assert _normalize("NEFT/RJPY2001/Acme Corp") == "NEFTRJPY2001ACMECORP"


def test_exact_reference_clean_match_closes_invoice():
    invoices = [_inv("INV1000", "Acme", "2026-08-01", 5000.0, "RJPY2001")]
    settlements = [_settle("TXN1", "2026-08-03", 5000.0, "NEFT/RJPY2001/Acme")]
    results = reconcile(invoices, settlements, use_llm=False)
    assert results[0]["status"] == "matched"
    assert results[0]["layer"] == "exact_reference+deduction_engine"
    assert invoices[0]["remaining_amount"] == 0.0
    assert invoices[0]["status"] == "closed"


def test_exact_reference_with_deduction_closes_and_records_formula():
    gross = 10_000.0
    net = 9_764.0  # 2% fee + 18% GST on fee, from the PRD worked example
    invoices = [_inv("INV1000", "Acme", "2026-08-01", gross, "RJPY2001")]
    settlements = [_settle("TXN1", "2026-08-03", net, "NEFT/RJPY2001/Acme")]
    results = reconcile(invoices, settlements, use_llm=False)
    assert results[0]["status"] == "matched"
    assert results[0]["deduction_label"] is not None
    assert "gateway_fee" in results[0]["deduction_label"]


def test_partial_payment_keeps_invoice_open_until_fully_paid():
    invoices = [_inv("INV1000", "Acme", "2026-08-01", 10_000.0, "RJPY2001")]
    first = [_settle("TXN1", "2026-08-02", 4_000.0, "NEFT/RJPY2001/Acme")]
    first_results = reconcile(invoices, first, use_llm=False)
    assert first_results[0]["layer"] == "exact_reference+partial_payment"
    assert invoices[0]["status"] == "partial"          # not yet fully paid
    assert invoices[0]["remaining_amount"] == 6_000.0

    second = [_settle("TXN2", "2026-08-05", 6_000.0, "NEFT/RJPY2001/Acme")]
    second_results = reconcile(invoices, second, use_llm=False)
    assert second_results[0]["status"] == "matched"
    assert invoices[0]["status"] == "closed"
    assert invoices[0]["remaining_amount"] == 0.0


def test_batched_settlement_closes_all_member_invoices():
    invoices = [
        _inv("INV1000", "Acme", "2026-08-01", 3_000.0, "RJPY2001"),
        _inv("INV1001", "Acme", "2026-08-01", 4_000.0, "RJPY2002"),
    ]
    settlements = [_settle("TXN1", "2026-08-03", 7_000.0, "Settlement batch RJPY2001 + RJPY2002")]
    results = reconcile(invoices, settlements, use_llm=False)
    assert results[0]["status"] == "matched"
    assert set(results[0]["matched_invoice_ids"]) == {"INV1000", "INV1001"}
    assert all(i["status"] == "closed" for i in invoices)


def test_genuinely_unpaid_invoice_is_never_force_matched():
    invoices = [_inv("INV1000", "Acme", "2026-08-01", 5_000.0, "RJPY2001")]
    settlements = [_settle("TXN1", "2026-08-03", 999.0, "Misc credit, unrelated")]
    results = reconcile(invoices, settlements, use_llm=False)
    # the unrelated credit must not close the unrelated invoice
    assert invoices[0]["status"] == "open"
    assert "INV1000" not in results[0]["matched_invoice_ids"]


def test_closing_arithmetic_always_sums_to_total():
    invoices = [_inv(f"INV{i}", "Acme", "2026-08-01", 1000.0 * (i + 1), f"RJPY200{i}") for i in range(5)]
    settlements = [_settle(f"TXN{i}", "2026-08-05", 500.0, f"Unrelated credit {i}") for i in range(5)]
    results = reconcile(invoices, settlements, use_llm=False)
    metrics = compute_metrics(results)
    assert metrics["matched"] + metrics["pending_confirmation"] + metrics["exception"] == metrics["total_settlements"]
    assert metrics["total_settlements"] == len(settlements)


def test_default_reconcile_matches_all_layers_enabled_explicitly():
    """The ablation knob added for evaluate.py must never change default
    behavior -- this pins that guarantee down as a real regression test."""
    from recon_agent.matcher import ALL_LAYERS

    def make_batch():
        invoices = [_inv(f"INV{i}", "Acme", "2026-08-01", 1000.0 * (i + 1), f"RJPY200{i}") for i in range(6)]
        settlements = [_settle(f"TXN{i}", "2026-08-05", 1000.0 * (i + 1) * 0.98, f"NEFT/RJPY200{i}/Acme")
                      for i in range(6)]
        return invoices, settlements

    inv_a, set_a = make_batch()
    default_results = reconcile(inv_a, set_a, use_llm=False)

    inv_b, set_b = make_batch()
    explicit_results = reconcile(inv_b, set_b, use_llm=False, enabled_layers=ALL_LAYERS)

    assert default_results == explicit_results
    assert [i["remaining_amount"] for i in inv_a] == [i["remaining_amount"] for i in inv_b]
