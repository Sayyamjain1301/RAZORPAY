"""Per-merchant fee schedule lookup (item 6): the default (no config, or an
unrecognized merchant_id) must reproduce matcher.py's fixed rate list
exactly -- that's what keeps this an additive feature, not a behavior change."""
import csv
import os

from recon_agent.fee_schedule import load_fee_schedule

DEFAULTS = dict(default_gateway_fee_rates=[0.018, 0.02], default_tds_rates=[0.01], default_gst_on_fee=0.18)


def test_missing_csv_falls_back_to_fixed_list(tmp_path):
    sched = load_fee_schedule(str(tmp_path / "nope.csv"), "any_merchant", **DEFAULTS)
    assert sched.source == "fixed_fallback"
    assert sched.gateway_fee_rates == [0.018, 0.02]


def test_unknown_merchant_id_falls_back(tmp_path):
    p = tmp_path / "schedules.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["merchant_id", "gateway_fee_rates", "tds_rates", "gst_on_fee"])
        w.writerow(["merchant_a", "0.01", "0.01", "0.18"])
    sched = load_fee_schedule(str(p), "merchant_b", **DEFAULTS)
    assert sched.source == "fixed_fallback"


def test_known_merchant_id_overrides_rates(tmp_path):
    p = tmp_path / "schedules.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["merchant_id", "gateway_fee_rates", "tds_rates", "gst_on_fee"])
        w.writerow(["merchant_a", "0.015;0.019", "0.001;0.02", "0.18"])
    sched = load_fee_schedule(str(p), "merchant_a", **DEFAULTS)
    assert sched.source == "merchant_schedule"
    assert sched.gateway_fee_rates == [0.015, 0.019]
    assert sched.tds_rates == [0.001, 0.02]


def test_malformed_row_fails_safe_to_fixed_list(tmp_path):
    p = tmp_path / "schedules.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["merchant_id", "gateway_fee_rates", "tds_rates", "gst_on_fee"])
        w.writerow(["merchant_a", "not-a-number", "0.01", "0.18"])
    sched = load_fee_schedule(str(p), "merchant_a", **DEFAULTS)
    assert sched.source == "fixed_fallback"


def test_default_matcher_config_reproduces_fixed_list():
    """The real config/merchant_fee_schedules.csv shipped in this repo must
    NOT change behavior for the default merchant_id matcher.py actually uses."""
    from recon_agent.matcher import (DEDUCTION_HYPOTHESES, GATEWAY_FEE_RATES, GST_ON_FEE,
                                     TDS_RATES, build_deduction_hypotheses)
    expected = build_deduction_hypotheses(GATEWAY_FEE_RATES, TDS_RATES, GST_ON_FEE)
    assert DEDUCTION_HYPOTHESES == expected, (
        "matcher.py's module-level DEDUCTION_HYPOTHESES must equal the fixed-list build "
        "for RECON_MERCHANT_ID='default' -- if this fails, the shipped example config "
        "is accidentally being picked up for the default merchant and silently changing "
        "every deterministic layer's behavior."
    )
