"""Grouping helpers: grouped bulk approval (#6) and anomaly grouping (#10).

Both take the *enriched* settlement result list — the raw txn_date/amount/
narration joined back in by app.py, since matcher.py's result dicts don't
carry them (matcher.py is left untouched; the join happens at the UI layer).
"""
from __future__ import annotations

from collections import defaultdict


def group_for_bulk_approval(pending: list[dict]) -> list[dict]:
    """Cluster pending_confirmation rows by (layer, deduction bucket) —
    the PRD's "Razorpay MDR+GST @2.36%, 213 items" pattern. Falls back to
    grouping by layer + rounded confidence band when there's no deduction
    label (e.g. investigator proposals with no deduction detected)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in pending:
        label = s.get("deduction_label") or "no deduction detected"
        key = (s["layer"], label)
        groups[key].append(s)
    out = []
    for (layer, label), items in groups.items():
        out.append({
            "layer": layer, "deduction_label": label, "items": items,
            "count": len(items),
            "txn_ids": [i["txn_id"] for i in items],
            "avg_confidence": round(sum(i["confidence"] for i in items) / len(items), 1),
        })
    out.sort(key=lambda g: -g["count"])
    return out


def group_by_customer(rows: list[dict], enriched: dict[str, dict]) -> dict[str, list[dict]]:
    """Group exceptions/pending rows sharing a counterparty — Ledge's
    "anomaly grouping": one customer with 6 exceptions reads as one root
    cause, not six unrelated rows."""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        info = enriched.get(r["txn_id"], {})
        key = info.get("narration_customer_guess") or "(unattributed)"
        out[key].append(r)
    return {k: v for k, v in out.items() if len(v) > 1}   # only real clusters, size 1 isn't an anomaly


def group_by_date(rows: list[dict], enriched: dict[str, dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        info = enriched.get(r["txn_id"], {})
        key = info.get("txn_date", "(unknown date)")
        out[key].append(r)
    return {k: v for k, v in out.items() if len(v) > 1}
