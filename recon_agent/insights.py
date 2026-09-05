"""Value-weighted analytics for the Overview page.

Everything the console reported until now was *record-count* weighted:
"85% auto-matched" means 85% of rows, not 85% of rupees. Standard
reconciliation practice treats that as the headline gap — ten small
exceptions carry less risk than one large unresolved variance, so a
controller reads the unreconciled *balance* first and the row count
second. These helpers compute the money view from data the pipeline
already produces (settlement amounts joined in by app.py's
enrich_settlements, plus matched invoice amounts), so nothing here
invents a number the run didn't actually measure.

Pure functions, no Streamlit import — so they're unit-testable and can't
accidentally depend on session state.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

# Matches the deduction labels matcher.py emits, e.g.
#   "gateway_fee(2.0%)+gst(18.0%_on_fee)"  /  "tds(2%)"
_FEE_LABEL = re.compile(r"gateway_fee\(([\d.]+)%\)\+gst\(([\d.]+)%_on_fee\)")
_TDS_LABEL = re.compile(r"tds\((\d+)%\)")


def _amount_of(txn_id: str, enriched: dict[str, dict]) -> float:
    return float(enriched.get(txn_id, {}).get("amount") or 0.0)


def value_summary(settlements: list[dict], enriched: dict[str, dict],
                  status_of) -> dict:
    """Rupee-weighted split of the batch by outcome, alongside the
    record-weighted split, so the two can be compared directly.

    `status_of(settlement) -> str` is passed in rather than read off the
    row, because app.py's effective_status() folds in this session's
    confirms/rejects/auto-confirm rules — the money view must reflect
    what the user has actually accepted, not the raw pipeline output.

    The divergence between record-% and value-% is the point: when
    value-matched trails record-matched, the batch's big-ticket items are
    disproportionately the unresolved ones, which is exactly the
    situation a count-only dashboard hides.
    """
    by_status_value: dict[str, float] = defaultdict(float)
    by_status_count: dict[str, int] = defaultdict(int)
    for s in settlements:
        st = status_of(s)
        by_status_value[st] += _amount_of(s["txn_id"], enriched)
        by_status_count[st] += 1

    total_value = sum(by_status_value.values())
    total_count = sum(by_status_count.values())

    def share(bucket: dict, key: str, total) -> float:
        return (bucket.get(key, 0) / total) if total else 0.0

    matched_value_pct = share(by_status_value, "matched", total_value)
    matched_count_pct = share(by_status_count, "matched", total_count)

    return {
        "total_value": total_value,
        "total_count": total_count,
        "matched_value": by_status_value.get("matched", 0.0),
        "pending_value": by_status_value.get("pending_confirmation", 0.0),
        "exception_value": by_status_value.get("exception", 0.0),
        "matched_count": by_status_count.get("matched", 0),
        "pending_count": by_status_count.get("pending_confirmation", 0),
        "exception_count": by_status_count.get("exception", 0),
        "matched_value_pct": matched_value_pct,
        "matched_count_pct": matched_count_pct,
        # positive => rupees are reconciling BETTER than rows (the
        # unresolved tail is small-ticket); negative => the expensive
        # items are the ones stuck.
        "value_vs_count_pp": (matched_value_pct - matched_count_pct) * 100,
        # everything not fully reconciled is exposure, whether it's
        # awaiting a click or genuinely unexplained.
        "at_risk_value": by_status_value.get("pending_confirmation", 0.0)
                        + by_status_value.get("exception", 0.0),
    }


def deduction_leakage(settlements: list[dict], invoices: list[dict],
                      enriched: dict[str, dict], status_of) -> dict:
    """Total rupees lost to gateway fees, GST on those fees, and TDS across
    everything that actually reconciled.

    Only settlements whose deduction label carries a real formula are
    counted. Partial payments are excluded on purpose: there the invoice's
    `amount` is the full original total, not what this one settlement
    covered, so a gross->net split would be arithmetically wrong (the same
    guard app.py's "Show the math" panel applies per-row).
    """
    inv_by_id = {i["invoice_id"]: i for i in invoices}
    fees = gst = tds = gross_total = 0.0
    n_with_deduction = 0

    for s in settlements:
        if status_of(s) != "matched":
            continue
        label = s.get("deduction_label")
        if not label or label == "none":
            continue
        if s["layer"] == "exact_reference+partial_payment":
            continue
        gross = sum(float(inv_by_id[i]["amount"]) for i in s["matched_invoice_ids"]
                   if i in inv_by_id)
        if gross <= 0:
            continue

        m_fee = _FEE_LABEL.match(label)
        m_tds = _TDS_LABEL.match(label)
        if m_fee:
            fee_rate, gst_rate = float(m_fee.group(1)) / 100, float(m_fee.group(2)) / 100
            fee = round(gross * fee_rate, 2)
            fees += fee
            gst += round(fee * gst_rate, 2)
        elif m_tds:
            tds += round(gross * float(m_tds.group(1)) / 100, 2)
        else:
            continue  # an unrecognized label shape contributes nothing rather than a guess
        gross_total += gross
        n_with_deduction += 1

    deducted = fees + gst + tds
    return {
        "gross": gross_total,
        "gateway_fees": fees,
        "gst_on_fees": gst,
        "tds": tds,
        "total_deducted": deducted,
        "net_received": gross_total - deducted,
        "effective_rate": (deducted / gross_total) if gross_total else 0.0,
        "n_settlements": n_with_deduction,
    }


AGING_BUCKETS = [(0, 7, "0-7 days"), (8, 14, "8-14 days"),
                 (15, 30, "15-30 days"), (31, 10**6, "30+ days")]


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def aging(unresolved: list[dict], enriched: dict[str, dict],
          today: date | None = None) -> dict:
    """Age unresolved items by days since the settlement actually landed.

    Deliberately measured from the settlement's own txn_date, not from
    when this tool first noticed it (ops.first_seen): a credit that hit
    the bank five weeks ago is five weeks old regardless of when someone
    got around to running a reconciliation, and that's the number a
    controller is accountable for.
    """
    today = today or date.today()
    buckets = {label: {"count": 0, "value": 0.0} for _, _, label in AGING_BUCKETS}
    undated = {"count": 0, "value": 0.0}
    oldest_days = 0

    for row in unresolved:
        info = enriched.get(row["txn_id"], {})
        d = _parse_date(info.get("txn_date"))
        amount = _amount_of(row["txn_id"], enriched)
        if d is None:
            undated["count"] += 1
            undated["value"] += amount
            continue
        age = max(0, (today - d).days)
        oldest_days = max(oldest_days, age)
        for lo, hi, label in AGING_BUCKETS:
            if lo <= age <= hi:
                buckets[label]["count"] += 1
                buckets[label]["value"] += amount
                break

    return {"buckets": buckets, "undated": undated, "oldest_days": oldest_days,
            "total_count": len(unresolved),
            "total_value": sum(_amount_of(r["txn_id"], enriched) for r in unresolved)}


def top_exposure(unresolved: list[dict], enriched: dict[str, dict],
                 limit: int = 5) -> list[dict]:
    """The largest unresolved amounts, biggest first — the worklist a
    controller should clear before a long tail of small ones."""
    rows = [{
        "txn_id": r["txn_id"],
        "amount": _amount_of(r["txn_id"], enriched),
        "status": r["status"],
        "layer": r["layer"],
        "confidence": r.get("confidence", 0),
        "counterparty": enriched.get(r["txn_id"], {}).get("narration_customer_guess"),
        "txn_date": enriched.get(r["txn_id"], {}).get("txn_date"),
    } for r in unresolved]
    rows.sort(key=lambda r: -r["amount"])
    return rows[:limit]


def concentration(unresolved: list[dict], enriched: dict[str, dict],
                  top_n: int = 5) -> float:
    """What share of total unresolved value sits in the top N items.

    A high number is good news operationally — it means clearing a
    handful of rows removes most of the exposure.
    """
    total = sum(_amount_of(r["txn_id"], enriched) for r in unresolved)
    if total <= 0:
        return 0.0
    top = sum(r["amount"] for r in top_exposure(unresolved, enriched, top_n))
    return top / total
