"""
Core reconciliation pipeline for the AI Finance Controller agent.

Design (see README.md for the full rationale / competitive positioning):

  Layer 1 - Exact reference match          )
  Layer 2 - Deduction-hypothesis engine    )  fully deterministic, zero LLM
  Layer 3 - Batch subset-sum reconciliation)  calls, 100% reproducible,
  Layer 4 - Partial-payment accumulation   )  auto-closes the ledger.

  Layer 5 - Tier-1 Exception Investigator (recon_agent.llm_reasoner):
            only reached by whatever survives layers 1-4. Read-only,
            proposal-only - never auto-closes anything. Every output from
            this layer is surfaced as "pending_confirmation" and needs an
            explicit human click before it affects any invoice balance.

This module never imports anything from data_gen.py - the deduction
formulas below are the agent's own domain knowledge (gateway fee + GST,
TDS), independent of however the synthetic data happened to be generated.
"""

from __future__ import annotations

import csv
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from typing import Optional

try:
    from rapidfuzz import fuzz
except ImportError:
    # No official Pyodide/wasm wheel for this C extension -- only reached
    # inside the in-browser stlite build (web/), never on the real server.
    # See _fuzz_fallback.py's module docstring.
    from ._fuzz_fallback import fuzz

from .fee_schedule import load_fee_schedule
from .llm_reasoner import investigate

# ---- Domain knowledge: known Indian payment-rail deduction formulas ----
# These stay the module's fixed fallback list, used whenever no per-merchant
# schedule applies (item 6) -- unchanged from before that feature existed,
# so a deployment with no merchant_fee_schedules.csv behaves identically.
GATEWAY_FEE_RATES = [0.018, 0.02, 0.023, 0.025, 0.03]
GST_ON_FEE = 0.18
TDS_RATES = [0.01, 0.02, 0.10]

MERCHANT_ID = os.environ.get("RECON_MERCHANT_ID", "default")
MERCHANT_FEE_SCHEDULE_CSV = os.path.join(os.path.dirname(__file__), "..", "config",
                                         "merchant_fee_schedules.csv")


def build_deduction_hypotheses(gateway_fee_rates: list[float], tds_rates: list[float],
                               gst_on_fee: float) -> list[tuple[float, str]]:
    hypotheses: list[tuple[float, str]] = [(0.0, "none")]
    for f in gateway_fee_rates:
        rate = round(f * (1 + gst_on_fee), 5)
        hypotheses.append((rate, f"gateway_fee({f*100:.1f}%)+gst({gst_on_fee*100:.0f}%_on_fee)"))
    for t in tds_rates:
        hypotheses.append((t, f"tds({t*100:.0f}%)"))
    return hypotheses


_active_schedule = load_fee_schedule(
    MERCHANT_FEE_SCHEDULE_CSV, MERCHANT_ID,
    default_gateway_fee_rates=GATEWAY_FEE_RATES, default_tds_rates=TDS_RATES,
    default_gst_on_fee=GST_ON_FEE,
)
DEDUCTION_HYPOTHESES: list[tuple[float, str]] = build_deduction_hypotheses(
    _active_schedule.gateway_fee_rates, _active_schedule.tds_rates, _active_schedule.gst_on_fee)

# ---- Layer 3 tuning -- configurable, item 7: change these, not the call
# sites, to widen/narrow the subset-sum search. -----------------------------
SUBSET_SUM_MAX_COMBO = 3          # max invoices considered per settlement
SUBSET_SUM_DATE_WINDOW_BACK = 2   # settlement slightly before invoice date (rare, clock skew)
SUBSET_SUM_DATE_WINDOW_FWD = 20   # settlement up to 20 days after invoice date
SUBSET_SUM_ANCHOR_FUZZY_THRESHOLD = 70  # min fuzzy score to treat a ref as a soft anchor


def _normalize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _tolerance(amount: float) -> float:
    return max(0.02, amount * 0.0006)


def check_deduction(target_amount: float, paid_amount: float) -> Optional[tuple[float, str]]:
    """Test known deduction formulas; return (rate, label) of the closest fit within tolerance."""
    if target_amount <= 0:
        return None
    best = None
    best_diff = None
    tol = _tolerance(target_amount)
    for rate, label in DEDUCTION_HYPOTHESES:
        expected_paid = round(target_amount * (1 - rate), 2)
        diff = abs(expected_paid - paid_amount)
        if diff <= tol and (best_diff is None or diff < best_diff):
            best, best_diff = (rate, label), diff
    return best


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def load_invoices(path: str) -> list[dict]:
    invoices = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            invoices.append({
                "invoice_id": row["invoice_id"],
                "customer": row["customer"],
                "invoice_date": row["invoice_date"],
                "amount": float(row["amount"]),
                "remaining_amount": float(row["amount"]),
                "reference_code": row["reference_code"],
                "status": "open",  # open -> partial -> closed
            })
    return invoices


def load_settlements(path: str) -> list[dict]:
    settlements = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            settlements.append({
                "txn_id": row["txn_id"],
                "txn_date": row["txn_date"],
                "amount": float(row["amount"]),
                "narration": row["narration"],
            })
    settlements.sort(key=lambda s: s["txn_date"])
    return settlements


# --------------------------------------------------------------------------
# Fuzzy scoring (feeds both the rule-based fallback and the LLM investigator)
# --------------------------------------------------------------------------

def score_candidate(inv: dict, settlement: dict) -> dict:
    narration_upper = settlement["narration"].upper()
    narration_compact = _normalize(settlement["narration"])
    ref_norm = _normalize(inv["reference_code"])

    ref_score = fuzz.partial_ratio(ref_norm, narration_compact)
    name_score = fuzz.token_sort_ratio(inv["customer"].upper(), narration_upper)
    text_score = max(ref_score, name_score)

    remaining = inv["remaining_amount"]
    if remaining <= 0:
        amount_score = 0.0
    else:
        ratio = settlement["amount"] / remaining
        if 0.80 <= ratio <= 1.02:
            amount_score = max(0.0, min(100.0, 100 - abs(1 - ratio) * 300))
        else:
            amount_score = max(0.0, 100 - abs(1 - ratio) * 150)

    days_diff = (_parse_date(settlement["txn_date"]) - _parse_date(inv["invoice_date"])).days
    if days_diff < 0:
        date_score = max(0.0, 60 + days_diff * 10)
    elif days_diff <= 10:
        date_score = 100 - days_diff * 5
    else:
        date_score = max(0.0, 100 - days_diff * 8)

    composite = 0.5 * text_score + 0.3 * amount_score + 0.2 * date_score

    return {
        "invoice_id": inv["invoice_id"],
        "customer": inv["customer"],
        "invoice_date": inv["invoice_date"],
        "amount": inv["amount"],
        "remaining_amount": inv["remaining_amount"],
        "reference_code": inv["reference_code"],
        "ref_score": text_score,
        "amount_score": amount_score,
        "date_score": date_score,
        "composite_score": round(composite, 1),
    }


def build_fuzzy_candidates(open_invoices: list[dict], settlement: dict, top_n: int = 3) -> list[dict]:
    scored = [score_candidate(inv, settlement) for inv in open_invoices if inv["remaining_amount"] > 0]
    scored.sort(key=lambda c: c["composite_score"], reverse=True)
    return scored[:top_n]


# --------------------------------------------------------------------------
# Layer 3: batch / subset-sum reconciliation (also covers single-invoice,
# no-reference "dropped narration" cases as the size-1 combo case)
# --------------------------------------------------------------------------

def try_anchored_completion(exact_candidates: list[dict], open_invoices: list[dict], settlement: dict):
    """When 1+ reference codes are confirmed by exact substring match but the
    confirmed total doesn't reconcile, the batch likely has 1-2 more members
    whose codes were garbled past exact matching. Rather than blindly
    subset-summing the entire ledger (which collides constantly - these
    reference codes share a common prefix and only differ in a couple of
    digits, so unrelated codes score deceptively high on generic text
    similarity), we anchor on the confirmed member(s), strip their codes out
    of the narration, and only score *remaining* narration text against
    *remaining* candidates. That residual-text signal is what disambiguates
    correctly in practice.
    """
    settle_date = _parse_date(settlement["txn_date"])
    narration_compact = _normalize(settlement["narration"])
    residual_text = narration_compact
    for inv in exact_candidates:
        residual_text = residual_text.replace(_normalize(inv["reference_code"]), "")

    pool = [
        inv for inv in open_invoices
        if inv not in exact_candidates and inv["remaining_amount"] > 0
        and -SUBSET_SUM_DATE_WINDOW_BACK <= (settle_date - _parse_date(inv["invoice_date"])).days <= SUBSET_SUM_DATE_WINDOW_FWD
    ]
    base_sum = sum(inv["remaining_amount"] for inv in exact_candidates)

    scored_options = []  # (score, extra_invoices, rate, label)
    for extra_size in (1, 2):
        if extra_size > len(pool):
            continue
        for combo in combinations(pool, extra_size):
            total = round(base_sum + sum(c["remaining_amount"] for c in combo), 2)
            ded = check_deduction(total, settlement["amount"])
            if ded is None:
                continue
            sims = [fuzz.partial_ratio(_normalize(c["reference_code"]), residual_text) for c in combo]
            avg_sim = sum(sims) / len(sims)
            if avg_sim >= 65:  # floor: every added invoice needs some textual echo in the leftover narration
                scored_options.append((avg_sim, list(combo), ded[0], ded[1]))

    if not scored_options:
        return None
    scored_options.sort(key=lambda o: o[0], reverse=True)
    best = scored_options[0]
    second_score = scored_options[1][0] if len(scored_options) > 1 else -1
    if best[0] - second_score < 10 and len(scored_options) > 1:
        return None  # too close to call deterministically - defer to the investigator

    _, extra_invoices, rate, label = best
    return exact_candidates + extra_invoices, rate, label


def _fuzzy_anchor(pool: list[dict], narration_compact: str) -> Optional[dict]:
    """Layer 3's anchor-first discipline (already used by batch completion),
    extended to the general subset-sum path: before falling back to a blind
    combinatorial search, check whether exactly one invoice's reference code
    has a strong-but-imperfect textual echo in the narration (a garbled or
    partially-truncated code, not a clean substring -- that would already
    have been caught as an exact_candidate upstream). One unambiguous fuzzy
    anchor narrows the search to its supersets, which is strictly safer than
    searching the whole pool blind. Returns None (not one, or none at all)
    when this doesn't apply, so the original blind search remains the
    fallback for genuinely reference-free narrations."""
    scored = [(fuzz.partial_ratio(_normalize(inv["reference_code"]), narration_compact), inv)
             for inv in pool]
    strong = [(s, inv) for s, inv in scored if s >= SUBSET_SUM_ANCHOR_FUZZY_THRESHOLD]
    if len(strong) != 1:
        return None
    return strong[0][1]


def try_subset_sum(open_invoices: list[dict], settlement: dict,
                   *, max_combo: int = SUBSET_SUM_MAX_COMBO,
                   date_window_back: int = SUBSET_SUM_DATE_WINDOW_BACK,
                   date_window_fwd: int = SUBSET_SUM_DATE_WINDOW_FWD):
    settle_date = _parse_date(settlement["txn_date"])
    pool = [
        inv for inv in open_invoices
        if inv["remaining_amount"] > 0
        and -date_window_back <= (settle_date - _parse_date(inv["invoice_date"])).days <= date_window_fwd
    ]
    # Prioritize invoices whose date is closest to the settlement date so that,
    # if the pool is large, truncation drops the least-plausible candidates first.
    pool.sort(key=lambda inv: abs((settle_date - _parse_date(inv["invoice_date"])).days))
    pool = pool[:26]  # keep combinatorics bounded (C(26,3) ~= 2600 combos x ~9 hypotheses)

    # ---- anchor-first: try a fuzzy-anchored search before the blind one ----
    narration_compact = _normalize(settlement["narration"])
    anchor = _fuzzy_anchor(pool, narration_compact)
    if anchor is not None:
        rest = [inv for inv in pool if inv is not anchor]
        residual_text = narration_compact.replace(_normalize(anchor["reference_code"]), "")
        anchored_matches = []
        for extra_size in range(0, max_combo):
            if extra_size > len(rest):
                continue
            for combo in combinations(rest, extra_size):
                total = round(anchor["remaining_amount"] + sum(c["remaining_amount"] for c in combo), 2)
                ded = check_deduction(total, settlement["amount"])
                if ded is None:
                    continue
                if combo:
                    sims = [fuzz.partial_ratio(_normalize(c["reference_code"]), residual_text) for c in combo]
                    if sum(sims) / len(sims) < 65:
                        continue
                anchored_matches.append(([anchor] + list(combo), ded[0], ded[1]))
        if len(anchored_matches) == 1:
            combo, rate, label = anchored_matches[0]
            return list(combo), rate, label
        if len(anchored_matches) > 1:
            return None  # anchored but still ambiguous -- defer, don't guess

    # ---- fallback: blind bounded search, for narrations with zero textual
    # signal at all (the "reference dropped entirely" scenario this layer
    # exists for in the first place) ----
    matches = []  # (combo, rate, label)
    for size in range(1, max_combo + 1):
        for combo in combinations(pool, size):
            total = round(sum(inv["remaining_amount"] for inv in combo), 2)
            ded = check_deduction(total, settlement["amount"])
            if ded is not None:
                matches.append((combo, ded[0], ded[1]))

    if len(matches) == 1:
        combo, rate, label = matches[0]
        return list(combo), rate, label
    return None  # zero or ambiguous (multiple) matches -> defer to the investigator layer


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _result(txn_id, status, layer, invoice_ids, confidence, rationale,
            deduction_rate=None, deduction_label=None, source="deterministic",
            input_tokens=0, output_tokens=0, llm_attempts=0, llm_path="n/a"):
    return {
        "txn_id": txn_id,
        "status": status,  # "matched" | "pending_confirmation" | "exception"
        "layer": layer,
        "matched_invoice_ids": invoice_ids,
        "confidence": confidence,
        "deduction_rate": deduction_rate,
        "deduction_label": deduction_label,
        "rationale": rationale,
        "source": source,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "llm_attempts": llm_attempts,   # item 4: visible in the audit log
        "llm_path": llm_path,           # succeeded_first_try / retried_then_succeeded / ...
    }


ALL_LAYERS = frozenset({"L1", "L2", "L3", "L4", "L5"})


def reconcile(invoices: list[dict], settlements: list[dict], use_llm: bool = True,
             enabled_layers: frozenset[str] = ALL_LAYERS, on_llm_retry=None) -> list[dict]:
    """`enabled_layers` is an ablation knob for evaluate.py only — the default
    (every layer on) is byte-identical to this function's original behavior;
    it never changes what a normal run does. L1=exact reference match,
    L2=deduction-formula fitting, L3=anchored batch/subset-sum, L4=partial
    payment, L5=Tier-1 investigator.

    `on_llm_retry` is optional (default None -- no behavior change) and is
    passed straight through to llm_reasoner.investigate() so app.py can
    surface "retrying (attempt 2/3)..." in the UI (item 4)."""
    results = []
    # One circuit breaker per run, shared across every L5 call in this batch
    # -- see llm_reasoner.investigate()'s docstring for why: a 429 on the
    # first unresolved settlement means every later one would hit the same
    # wall, and paying the full retry-and-backoff cost N times over instead
    # of once was measured to turn a sub-second run into a multi-minute one.
    llm_circuit: dict = {"open": False}

    for settlement in settlements:
        open_invoices = [inv for inv in invoices if inv["status"] != "closed"]
        narration_compact = _normalize(settlement["narration"])
        exact_candidates = [
            inv for inv in open_invoices
            if inv["remaining_amount"] > 0 and _normalize(inv["reference_code"]) in narration_compact
        ] if "L1" in enabled_layers else []

        result = None

        # --- Layer 1+2: single clean reference match, resolved via deduction hypothesis ---
        if len(exact_candidates) == 1:
            inv = exact_candidates[0]
            ded = check_deduction(inv["remaining_amount"], settlement["amount"]) if "L2" in enabled_layers else None
            if ded is not None:
                rate, label = ded
                inv["remaining_amount"] = 0.0
                inv["status"] = "closed"
                result = _result(
                    settlement["txn_id"], "matched", "exact_reference+deduction_engine",
                    [inv["invoice_id"]], 100,
                    f"Reference '{inv['reference_code']}' found in narration; amount reconciles via {label}.",
                    deduction_rate=rate, deduction_label=label,
                )
            elif "L4" in enabled_layers and \
                    inv["remaining_amount"] * 0.05 < settlement["amount"] < inv["remaining_amount"] * 0.98:
                # partial payment against this invoice
                inv["remaining_amount"] = round(inv["remaining_amount"] - settlement["amount"], 2)
                closed_now = inv["remaining_amount"] <= 0.5
                inv["status"] = "closed" if closed_now else "partial"
                if closed_now:
                    inv["remaining_amount"] = 0.0
                result = _result(
                    settlement["txn_id"], "matched", "exact_reference+partial_payment",
                    [inv["invoice_id"]], 100,
                    f"Reference '{inv['reference_code']}' found; settlement is a partial payment "
                    f"({'invoice now fully closed' if closed_now else 'balance still outstanding'}).",
                    deduction_rate=0.0, deduction_label="none",
                )
            # else: amount doesn't fit deduction or plausible-partial range (e.g. overpayment) -> fall through

        # --- Layer 1+2 batch variant: multiple reference codes in one narration ---
        elif len(exact_candidates) >= 2:
            total_remaining = round(sum(inv["remaining_amount"] for inv in exact_candidates), 2)
            ded = check_deduction(total_remaining, settlement["amount"]) if "L2" in enabled_layers else None
            if ded is not None:
                rate, label = ded
                for inv in exact_candidates:
                    inv["remaining_amount"] = 0.0
                    inv["status"] = "closed"
                result = _result(
                    settlement["txn_id"], "matched", "exact_reference_batch+deduction_engine",
                    [inv["invoice_id"] for inv in exact_candidates], 100,
                    f"{len(exact_candidates)} reference codes found in one settlement narration "
                    f"(batched payout); combined amount reconciles via {label}.",
                    deduction_rate=rate, deduction_label=label,
                )

        # --- Layer 3a: anchored batch completion (1+ codes confirmed, rest inferred) ---
        if result is None and exact_candidates and "L3" in enabled_layers:
            anchored = try_anchored_completion(exact_candidates, open_invoices, settlement)
            if anchored is not None:
                invs, rate, label = anchored
                for inv in invs:
                    inv["remaining_amount"] = 0.0
                    inv["status"] = "closed"
                result = _result(
                    settlement["txn_id"], "matched", "anchored_batch_completion",
                    [inv["invoice_id"] for inv in invs], 95,
                    f"{len(exact_candidates)} reference code(s) confirmed exactly in narration; "
                    f"remaining {len(invs) - len(exact_candidates)} batch member(s) inferred from leftover "
                    f"narration text matching their codes; total reconciles via {label}.",
                    deduction_rate=rate, deduction_label=label,
                )

        # --- Layer 3b: subset-sum, also covers reference-dropped single invoices ---
        if result is None and "L3" in enabled_layers:
            subset = try_subset_sum(open_invoices, settlement)
            if subset is not None:
                invs, rate, label = subset
                for inv in invs:
                    inv["remaining_amount"] = 0.0
                    inv["status"] = "closed"
                combo_desc = "single invoice" if len(invs) == 1 else f"{len(invs)}-invoice batch"
                result = _result(
                    settlement["txn_id"], "matched", "amount_date_subset_sum",
                    [inv["invoice_id"] for inv in invs], 97,
                    f"No usable reference text in narration; a unique {combo_desc} within the expected "
                    f"settlement date window reconciles exactly via {label}.",
                    deduction_rate=rate, deduction_label=label,
                )

        # --- Layer 5: Tier-1 Exception Investigator (bounded, read-only, proposal only) ---
        if result is None and "L5" in enabled_layers:
            candidates = build_fuzzy_candidates(open_invoices, settlement)
            inv_result = investigate(settlement, candidates, use_llm=use_llm, on_retry=on_llm_retry,
                                     circuit_breaker=llm_circuit)
            if inv_result.chosen_invoice_ids:
                # item 5: a proposal can now be a batch (>1 invoice_id), not
                # just the single top candidate -- surfaced with one combined
                # confidence and rationale, still pending_confirmation only.
                result = _result(
                    settlement["txn_id"], "pending_confirmation", "llm_investigator",
                    list(inv_result.chosen_invoice_ids), inv_result.confidence,
                    inv_result.rationale, source=inv_result.source,
                    input_tokens=inv_result.input_tokens, output_tokens=inv_result.output_tokens,
                    llm_attempts=inv_result.attempts, llm_path=inv_result.llm_path,
                )
            else:
                result = _result(
                    settlement["txn_id"], "exception", "llm_investigator",
                    [], inv_result.confidence, inv_result.rationale, source=inv_result.source,
                    input_tokens=inv_result.input_tokens, output_tokens=inv_result.output_tokens,
                    llm_attempts=inv_result.attempts, llm_path=inv_result.llm_path,
                )
        elif result is None:
            # L5 disabled for this ablation run and nothing upstream resolved
            # it -- record as an exception so closing arithmetic still holds.
            result = _result(
                settlement["txn_id"], "exception", "none",
                [], 0, "L5 disabled for this ablation run; no investigator was consulted.",
            )

        results.append(result)

    return results


# --------------------------------------------------------------------------
# Scoring against synthetic ground truth (for the dashboard's accuracy panel)
# --------------------------------------------------------------------------

def load_ground_truth(path: str) -> dict[str, set[str]]:
    """Returns txn_id -> set(invoice_id) it should map to (empty set = unexplained credit)."""
    true_map: dict[str, set[str]] = defaultdict(set)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            txns = [t for t in row["settlement_txn_ids"].split("|") if t]
            for t in txns:
                true_map[t].add(row["invoice_id"])
    return true_map


def load_deduction_truth(path: str) -> dict[str, tuple[float, str]]:
    truth = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            truth[row["txn_id"]] = (float(row["deduction_rate"]), row["deduction_label"])
    return truth


def compute_metrics(results: list[dict], ground_truth: Optional[dict] = None,
                     deduction_truth: Optional[dict] = None) -> dict:
    total = len(results)
    by_status = defaultdict(int)
    for r in results:
        by_status[r["status"]] += 1

    metrics = {
        "total_settlements": total,
        "matched": by_status["matched"],
        "pending_confirmation": by_status["pending_confirmation"],
        "exception": by_status["exception"],
        "auto_match_rate": round(by_status["matched"] / total, 4) if total else 0.0,
        "resolved_rate": round((by_status["matched"] + by_status["pending_confirmation"]) / total, 4) if total else 0.0,
    }

    if ground_truth is not None:
        precision_hits = precision_total = 0
        recall_hits = recall_total = 0
        for r in results:
            predicted = set(r["matched_invoice_ids"]) if r["status"] in ("matched", "pending_confirmation") else set()
            true = ground_truth.get(r["txn_id"], set())
            correct = predicted == true
            if predicted:
                precision_total += 1
                precision_hits += correct
            if true:
                recall_total += 1
                recall_hits += correct
        metrics["precision"] = round(precision_hits / precision_total, 4) if precision_total else None
        metrics["recall"] = round(recall_hits / recall_total, 4) if recall_total else None

    if deduction_truth is not None:
        checked = correct = 0
        for r in results:
            if r["status"] != "matched" or r["deduction_rate"] is None:
                continue
            truth = deduction_truth.get(r["txn_id"])
            if truth is None:
                continue
            checked += 1
            if abs(truth[0] - r["deduction_rate"]) < 0.005:
                correct += 1
        metrics["deduction_hypothesis_accuracy"] = round(correct / checked, 4) if checked else None

    return metrics


def run_reconciliation(invoices_csv: str, settlements_csv: str,
                        ground_truth_csv: Optional[str] = None,
                        deduction_truth_csv: Optional[str] = None,
                        use_llm: bool = True, on_llm_retry=None) -> dict:
    invoices = load_invoices(invoices_csv)
    settlements = load_settlements(settlements_csv)
    results = reconcile(invoices, settlements, use_llm=use_llm, on_llm_retry=on_llm_retry)

    ground_truth = load_ground_truth(ground_truth_csv) if ground_truth_csv else None
    deduction_truth = load_deduction_truth(deduction_truth_csv) if deduction_truth_csv else None
    metrics = compute_metrics(results, ground_truth, deduction_truth)

    return {"invoices": invoices, "settlements": results, "metrics": metrics}
