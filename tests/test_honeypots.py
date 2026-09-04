"""Adversarial honeypots (item 9): injected in data_gen.py, must never be
matched by the agent (correct behavior is to leave them as unexplained
credits), and evaluate.py must report the real bait-taken count."""
import os

from data_gen import generate
from evaluate import load_honeypots, run_once
from recon_agent.matcher import load_invoices, load_settlements, reconcile


def test_honeypots_are_generated_and_hidden(tmp_path):
    generate(60, 42, str(tmp_path))
    assert os.path.exists(tmp_path / "honeypots.csv")
    honeypot_ids = load_honeypots(str(tmp_path / "honeypots.csv"))
    assert len(honeypot_ids) >= 1

    settlement_ids = {r["txn_id"] for r in load_settlements(str(tmp_path / "settlements.csv"))}
    assert honeypot_ids.issubset(settlement_ids)  # real rows in the visible file


def test_honeypots_are_never_in_ground_truth(tmp_path):
    """A honeypot must never be the 'correct' answer for any invoice --
    the correct agent behavior is to decline it, not match it."""
    generate(60, 7, str(tmp_path))
    honeypot_ids = load_honeypots(str(tmp_path / "honeypots.csv"))
    from recon_agent.matcher import load_ground_truth
    gt = load_ground_truth(str(tmp_path / "ground_truth.csv"))
    all_true_txns = {t for txns in gt.values() for t in txns}
    assert honeypot_ids.isdisjoint(all_true_txns)


def test_run_once_reports_honeypot_stats(tmp_path):
    generate(60, 42, str(tmp_path))
    result = run_once(str(tmp_path), use_llm=False)
    assert "honeypots" in result
    assert result["honeypots"]["total"] >= 1
    assert result["honeypots"]["baited"] <= result["honeypots"]["total"]


def test_deterministic_layers_never_take_the_bait():
    """A honeypot has no reference code and no real invoice behind it -- L1-L4
    (deterministic) must never close it, by construction of what those layers
    require (an exact reference match or a unique amount+date subset-sum)."""
    invoices = [{"invoice_id": "INV1", "customer": "Acme", "invoice_date": "2026-08-01",
               "amount": 5000.0, "remaining_amount": 5000.0, "reference_code": "RJPY2001",
               "status": "open"}]
    # a honeypot: same amount as a plausible invoice, but no reference, and
    # crucially INV1 above is already closed by its own real settlement first
    settlements = [
        {"txn_id": "REAL", "txn_date": "2026-08-03", "amount": 5000.0, "narration": "NEFT/RJPY2001/Acme"},
        {"txn_id": "BAIT", "txn_date": "2026-08-04", "amount": 5000.0, "narration": "NEFT credit OtherCorp"},
    ]
    results = reconcile(invoices, settlements, use_llm=False)
    bait_result = next(r for r in results if r["txn_id"] == "BAIT")
    assert not bait_result["matched_invoice_ids"]  # INV1 already closed, nothing left to wrongly match


def test_forced_scenario_guarantees_batched_settlement(tmp_path):
    """item 7: requesting a scenario type guarantees it appears, even on a
    seed that might not have produced one naturally."""
    from data_gen import generate
    import csv

    generate(20, 999, str(tmp_path), force_scenarios={"batched_settlement"})
    with open(tmp_path / "ground_truth.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    # a batched settlement covers >=2 invoices under one settlement_txn_id
    txn_to_invoices: dict[str, int] = {}
    for row in rows:
        for txn in filter(None, row["settlement_txn_ids"].split("|")):
            txn_to_invoices[txn] = txn_to_invoices.get(txn, 0) + 1
    assert any(count >= 2 for count in txn_to_invoices.values()), \
        "expected at least one settlement covering 2+ invoices (a batch)"


def test_forced_scenario_guarantees_partial_payment(tmp_path):
    from data_gen import generate
    import csv

    generate(20, 999, str(tmp_path), force_scenarios={"partial_payment"})
    with open(tmp_path / "ground_truth.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    # a partial payment splits one invoice across 2 settlement txns
    assert any(len(row["settlement_txn_ids"].split("|")) >= 2 for row in rows if row["settlement_txn_ids"])


def test_no_force_scenarios_reproduces_default_output(tmp_path):
    """The additive parameter must not change output when unused."""
    from data_gen import generate

    d1, d2 = tmp_path / "a", tmp_path / "b"
    generate(30, 42, str(d1))
    generate(30, 42, str(d2), force_scenarios=None)
    assert (d1 / "invoices.csv").read_text() == (d2 / "invoices.csv").read_text()
    assert (d1 / "settlements.csv").read_text() == (d2 / "settlements.csv").read_text()
