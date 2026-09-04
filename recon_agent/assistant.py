"""A bounded Q&A assistant over one reconciliation run.

Same guardrail philosophy as llm_reasoner.py's Tier-1 Investigator: it is
handed a compact summary of the run (metrics + settlement results), told
never to invent facts outside that payload, and answers in plain finance
language. It has no write access to anything — it only ever returns text.

Graceful degradation matches the rest of the app: no ANTHROPIC_API_KEY, or a
failed call, falls back to a deterministic templated answer and labels the
source honestly (never lets a rule-based guess pass as a live model answer).
"""
from __future__ import annotations

import json
import os

DEFAULT_MODEL = os.environ.get("RECON_LLM_MODEL", "claude-3-5-haiku-20241022")

QUICK_QUESTIONS = [
    "Why is the auto-match rate not 100%?",
    "Which exceptions need my attention first?",
    "Explain the biggest deduction variance in this batch.",
]

SYSTEM_PROMPT = (
    "You are a finance-ops assistant embedded in a payment reconciliation console. "
    "You are given a compact JSON summary of ONE reconciliation run: its headline metrics "
    "and its settlement results (status, layer, confidence, deduction label, rationale). "
    "Answer the user's question using ONLY this data — never invent invoice IDs, amounts, "
    "customers, or reasons that are not present in it. If the data doesn't contain the "
    "answer, say so plainly instead of guessing. Keep answers to 2-4 sentences of plain "
    "finance language, no markdown headers, no bullet lists unless truly needed."
)


def _context_payload(metrics: dict, settlements: list[dict], max_rows: int = 60) -> dict:
    trimmed = [{
        "txn_id": s["txn_id"], "status": s["status"], "layer": s["layer"],
        "confidence": s["confidence"], "deduction_rate": s.get("deduction_rate"),
        "deduction_label": s.get("deduction_label"),
        "matched_invoice_ids": s["matched_invoice_ids"],
        "rationale": (s["rationale"] or "")[:220],
    } for s in settlements[:max_rows]]
    return {"metrics": metrics, "settlements": trimmed}


def _rule_based_answer(question: str, metrics: dict, settlements: list[dict]) -> str:
    q = question.lower()

    if ("pending" in q or "confirmation" in q) and ("under" in q or "below" in q) and "%" in question:
        import re
        m = re.search(r"(\d+)\s*%", question)
        threshold = int(m.group(1)) if m else 70
        matches = [s for s in settlements if s["status"] == "pending_confirmation"
                  and s["confidence"] < threshold]
        if not matches:
            return f"No pending confirmations are under {threshold}% confidence in this run."
        lines = "; ".join(f"{s['txn_id']} ({s['confidence']}%)" for s in matches)
        return f"{len(matches)} pending confirmation(s) under {threshold}% confidence: {lines}."

    if "honeypot" in q:
        return ("Honeypots are adversarial credits injected by data_gen.py: same amount as a "
               "real invoice, a different counterparty, and no reference code -- engineered to "
               "tempt the matcher into a false positive. See the Honeypots panel in Model "
               "diagnostics for how many were baited in this specific run.")

    if "decdet" in q:
        return ("DecDet (decision determinism) reruns the same batch on identical input N times "
               "and reports the fraction of decisions that landed the same way every time. It's "
               "the audit-replay guarantee: a deterministic run should score 100%; a live LLM "
               "call is not seeded, so it can legitimately score lower without that being a bug.")

    if "ablation" in q:
        return ("The ablation table disables one matching layer at a time and reruns the batch, "
               "showing each layer's real marginal contribution to the auto-match rate -- run it "
               "from the Model diagnostics tab to see this run's actual numbers per layer.")

    if "dry-run" in q or "dry run" in q:
        return ("A rule dry-run backtests a proposed confidence threshold or rule change against "
               "this batch's hidden ground truth before it's ever activated -- shows what would "
               "have auto-closed and whether that would have been correct, without changing "
               "anything live.")

    if "confidence threshold" in q:
        return ("The confidence threshold is swept across the full 0-100 range in Model "
               "diagnostics, plotting coverage (auto-approval rate) against precision at each "
               "point -- pick the point on that curve where the precision floor you're "
               "comfortable with meets the highest coverage.")

    if "auto-confirm" in q and "suggest-only" in q:
        return ("Suggest-only means every Tier-1 investigator proposal needs an explicit human "
               "click before it affects the ledger. Auto-confirm (with a confidence floor) lets "
               "proposals above that floor close automatically without a click -- configurable "
               "per rule in the sidebar, and every auto-confirmed row is still logged with which "
               "rule closed it.")

    if "partial payment" in q:
        partials = [s for s in settlements if s["layer"] == "exact_reference+partial_payment"]
        return (f"{len(partials)} settlement(s) in this run matched via partial payment -- the "
               f"invoice's remaining balance is reduced by the settlement amount and it stays "
               f"open until a later settlement closes it fully.")

    if "how many records" in q or "records in this run" in q:
        return f"This run processed {metrics.get('total_settlements', 0)} settlement records."

    if "still open" in q or "which invoices" in q:
        # settlements don't carry invoice status directly in this payload;
        # answer from what's derivable -- unmatched + partial counts
        unmatched = metrics.get("total_settlements", 0) - metrics.get("matched", 0)
        return (f"{unmatched} settlement(s) have not closed an invoice outright in this run "
               f"({metrics.get('pending_confirmation', 0)} pending confirmation, "
               f"{metrics.get('exception', 0)} exceptions) -- see the Ledger tab for exact "
               f"per-invoice open balances.")

    if "precision" in q and "recall" in q:
        return ("Precision is: of everything the agent proposed as a match, what fraction was "
               "actually correct. Recall is: of everything that truly had a match, what fraction "
               "the agent found. A false match corrupts the ledger silently; a missed match just "
               "sits in a queue a human was already reading — that asymmetry is why this system "
               "optimizes for a precision floor rather than a single blended score.")

    if "5 layer" in q or "each layer" in q or "each of the" in q:
        return ("Layer 1 matches an exact reference code found verbatim in the narration. Layer 2 "
               "tests known gateway-fee/GST/TDS formulas against the delta. Layer 3 handles batched "
               "or reference-dropped settlements via anchored subset-sum. Layer 4 tracks partial "
               "payments across multiple settlements. Layer 5 is the only LLM call — a read-only "
               "Tier-1 investigator that proposes a match for a human to confirm; it never writes "
               "to the ledger.")

    if "no api key" in q or "no key" in q or "without" in q and "key" in q:
        return ("Layer 5 falls back to a deterministic, explainable rule-based investigator instead "
               "of calling Claude — it picks the highest-scoring candidate if it clears a confidence "
               "bar, and says so honestly in the rationale, labelled 'rule_based_fallback' rather "
               "than pretending to be a model answer.")

    if "synthetic data" in q or "generated" in q:
        return ("data_gen.py builds invoices with randomized amounts and reference codes, then "
               "settlements that mirror real messiness: gateway-fee/GST/TDS deductions, garbled or "
               "dropped reference codes, batched payouts, split partial payments, and a handful of "
               "genuinely unpaid invoices and unexplained credits the agent must correctly leave "
               "alone rather than force-match.")

    if "explain" in q and "plain english" in q:
        txn_hit = next((s for s in settlements if s["txn_id"].upper() in question.upper()), None)
        if txn_hit:
            return txn_hit["rationale"]

    if "what would break" in q or "break this match" in q:
        txn_hit = next((s for s in settlements if s["txn_id"].upper() in question.upper()), None)
        if txn_hit:
            return (f"{txn_hit['txn_id']} matched via `{txn_hit['layer']}`. A different reference "
                   f"code, a deduction formula outside the known fee/GST/TDS rate list, or a "
                   f"settlement date outside the expected post-invoice window would all be enough "
                   f"to push this into the exception queue instead.")

    if "100" in q or "auto-match" in q or "match rate" in q:
        return (f"Auto-match rate is {metrics['auto_match_rate']*100:.1f}% because "
               f"{metrics['pending_confirmation']} settlement(s) needed the Tier-1 investigator's "
               f"proposal and {metrics['exception']} could not be resolved at all — usually a "
               f"garbled or missing reference, or a credit with no matching invoice.")

    if "attention" in q or "which exception" in q or "priorit" in q or "first" in q:
        exc = sorted([s for s in settlements if s["status"] == "exception"],
                    key=lambda s: -s["confidence"])
        if not exc:
            return "There are no exceptions in this run — everything is either matched or pending confirmation."
        top = exc[:3]
        lines = "; ".join(f"{s['txn_id']} (confidence {s['confidence']})" for s in top)
        return f"Highest-confidence exceptions worth a first look: {lines}."

    if "deduction" in q or "variance" in q or "fee" in q or "gst" in q or "tds" in q:
        ded = [s for s in settlements if s.get("deduction_rate")]
        if not ded:
            return "No deduction was detected on any matched settlement in this run."
        biggest = max(ded, key=lambda s: s["deduction_rate"])
        return (f"The largest deduction variance is on {biggest['txn_id']}: {biggest['deduction_label']}, "
               f"a {biggest['deduction_rate']*100:.2f}% reduction from the invoice total.")

    txn_hit = next((s for s in settlements if s["txn_id"].upper() in question.upper()), None)
    if txn_hit:
        return f"{txn_hit['txn_id']} — status {txn_hit['status']}, layer {txn_hit['layer']}. {txn_hit['rationale']}"

    return ("No ANTHROPIC_API_KEY set, so this is the rule-based fallback — it can answer "
           "questions about match rate, exception priority, or deduction variance. Try one "
           "of the quick questions, or ask about a specific txn ID.")


def ask(question: str, metrics: dict, settlements: list[dict], *, use_llm: bool = True) -> dict:
    """Returns {answer, source}. source is 'llm' or 'rule_based_fallback', never blurred."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not use_llm or not api_key:
        return {"answer": _rule_based_answer(question, metrics, settlements), "source": "rule_based_fallback"}

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        payload = _context_payload(metrics, settlements)
        message = client.messages.create(
            model=DEFAULT_MODEL, max_tokens=300, temperature=0.2, system=SYSTEM_PROMPT,
            messages=[{"role": "user",
                      "content": json.dumps(payload, default=str) + "\n\nQuestion: " + question}],
        )
        text = "".join(b.text for b in message.content if hasattr(b, "text")).strip()
        return {"answer": text or "(no answer returned)", "source": "llm"}
    except Exception as exc:  # noqa: BLE001 — must degrade gracefully, never crash the console
        fallback = _rule_based_answer(question, metrics, settlements)
        return {"answer": f"[live call failed: {exc.__class__.__name__}] {fallback}",
               "source": "rule_based_fallback"}
