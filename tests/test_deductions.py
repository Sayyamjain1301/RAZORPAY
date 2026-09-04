"""Deduction-hypothesis engine — every published formula, exact to the paisa
(well, to the rupee: this lineage's money model is plain float rupees, not
integer paise — see tests/test_money_model.py's note on why that's flagged,
not silently changed)."""
from recon_agent.matcher import GATEWAY_FEE_RATES, GST_ON_FEE, TDS_RATES, check_deduction


def test_worked_example_mdr_gst():
    # Rs 10,000 @ 2% gateway fee + 18% GST on that fee -> net Rs 9,764
    result = check_deduction(10_000.0, 9_764.0)
    assert result is not None
    rate, label = result
    assert abs(rate - 0.02 * 1.18) < 1e-6
    assert "gateway_fee(2.0%)" in label
    assert "gst(18%_on_fee)" in label


def test_gst_is_on_the_fee_not_the_invoice():
    fee_rate = 0.02
    total_rate = fee_rate * (1 + GST_ON_FEE)
    gross = 10_000.0
    net_correct = round(gross * (1 - total_rate), 2)
    net_wrong = round(gross * (1 - GST_ON_FEE), 2)  # 18% of the whole invoice -- wrong model
    assert net_correct != net_wrong
    assert check_deduction(gross, net_correct) is not None


def test_every_published_gateway_fee_rate():
    gross = 20_000.0
    for fee_rate in GATEWAY_FEE_RATES:
        total_rate = round(fee_rate * (1 + GST_ON_FEE), 5)
        net = round(gross * (1 - total_rate), 2)
        result = check_deduction(gross, net)
        assert result is not None, f"fee rate {fee_rate} did not resolve"
        assert abs(result[0] - total_rate) < 1e-4


def test_every_published_tds_rate():
    gross = 15_000.0
    for tds_rate in TDS_RATES:
        net = round(gross * (1 - tds_rate), 2)
        result = check_deduction(gross, net)
        assert result is not None, f"TDS rate {tds_rate} did not resolve"
        assert abs(result[0] - tds_rate) < 1e-4
        assert result[1] == f"tds({tds_rate*100:.0f}%)"


def test_no_deduction_clean_match():
    result = check_deduction(5_000.0, 5_000.0)
    assert result is not None
    assert result[1] == "none"


def test_no_fit_beyond_tolerance():
    # a delta that fits no published formula at all
    assert check_deduction(10_000.0, 5_000.0) is None


def test_zero_or_negative_target_returns_none():
    assert check_deduction(0.0, 0.0) is None
    assert check_deduction(-100.0, -50.0) is None
