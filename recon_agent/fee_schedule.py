"""Per-merchant fee schedule lookup (item 6).

This lineage's data model is one merchant's own books (invoices.csv is that
merchant's receivables; "customer" is who owes THEM money, not which
merchant is running the agent) -- so "keyed by merchant_id" means: which
merchant's negotiated MDR/TDS/GST terms this deployment should test against,
not a per-invoice or per-customer rate. A real aggregator runs one agent
instance per merchant, each with its own negotiated schedule.

Falls back to matcher.py's built-in fixed rate list whenever:
  - the schedule CSV doesn't exist at all, or
  - it exists but has no row for the configured merchant_id.
Either fallback is silent-but-labelled: `source` on the returned schedule
tells the caller which path was taken, so this is inspectable, not a guess.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass


@dataclass
class FeeSchedule:
    merchant_id: str
    gateway_fee_rates: list[float]
    tds_rates: list[float]
    gst_on_fee: float
    source: str  # "merchant_schedule" | "fixed_fallback"


def load_fee_schedule(csv_path: str, merchant_id: str, *,
                      default_gateway_fee_rates: list[float],
                      default_tds_rates: list[float],
                      default_gst_on_fee: float) -> FeeSchedule:
    fallback = FeeSchedule(merchant_id, list(default_gateway_fee_rates),
                          list(default_tds_rates), default_gst_on_fee, source="fixed_fallback")
    if not os.path.exists(csv_path):
        return fallback
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("merchant_id") != merchant_id:
                continue
            try:
                return FeeSchedule(
                    merchant_id=merchant_id,
                    gateway_fee_rates=[float(x) for x in row["gateway_fee_rates"].split(";") if x],
                    tds_rates=[float(x) for x in row["tds_rates"].split(";") if x],
                    gst_on_fee=float(row["gst_on_fee"]),
                    source="merchant_schedule",
                )
            except (KeyError, ValueError):
                # malformed row for this merchant -- fail safe to the fixed
                # list rather than crash the pipeline on a config typo
                return fallback
    return fallback
