"""
Tier-1 Exception Investigator.

This is the ONLY module in the whole agent that talks to an LLM, and it is
deliberately kept narrow: it never sees the full ledger, never computes
totals, and never writes anything back to invoice/settlement state. It is
handed one settlement plus a short-list of already-scored candidate
invoices (produced deterministically by matcher.py) and asked to do exactly
one thing: pick the most plausible candidate (or say "none of these") and
explain why in plain language, with a confidence score.

Guardrail: the caller (matcher.py) treats every response from this module as
a *proposal*, never a fact. Proposals are surfaced in the dashboard as
"pending confirmation" and require an explicit human click before an
invoice is ever marked closed. If no API key is configured, or the call
fails for any reason, we fall back to a deterministic, explainable
rule-based investigator so the pipeline never breaks and never blocks.

Provider: Google Gemini, via the `google-genai` SDK (`google.genai.Client`).
Reads GEMINI_API_KEY (falls back to GOOGLE_API_KEY, which the SDK itself
also recognizes natively).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


DEFAULT_MODEL = os.environ.get("RECON_LLM_MODEL", "gemini-3.6-flash")

CANDIDATE_THRESHOLD = 60  # min composite score to be proposable at all

# ---- retry/backoff (item 4): only transient failures are worth retrying --
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 0.5   # 0.5s, 1s, 2s -- kept short so a live demo isn't stalled

# google.genai.errors.APIError (and its ClientError/ServerError subclasses)
# carry an HTTP-style `.code` -- checked below by code, which survives SDK
# version/class-name churn better than isinstance against a specific
# exception hierarchy. Anything without a `.code` (a raw network-level
# httpx timeout/connection error, for instance) is matched by class name.
_TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_EXCEPTION_NAMES = frozenset({
    "ServerError", "TimeoutException", "ConnectError", "ConnectTimeout",
    "ReadTimeout", "ConnectionError", "TimeoutError",
})


def _is_transient(exc: Exception) -> bool:
    """Checked by HTTP code / class name, not isinstance -- this module must
    still work with the google-genai SDK not installed at all (it's imported
    lazily), and google.genai.errors.APIError.code carries the real signal
    for rate-limit (429) and server (5xx) errors regardless of SDK version."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _TRANSIENT_HTTP_CODES:
        return True
    return exc.__class__.__name__ in _TRANSIENT_EXCEPTION_NAMES

SYSTEM_PROMPT = (
    "You are a narrow, read-only finance-ops exception investigator. You are given one "
    "unmatched bank/gateway settlement transaction and a short list of candidate open "
    "invoices that a deterministic matching engine could not confidently resolve on its "
    "own (garbled reference codes, missing references, or ambiguous amounts). "
    "Usually exactly one candidate is the real match. Occasionally the settlement is a "
    "single payout covering MULTIPLE invoices from the candidate list (a batch), in which "
    "case you should propose all of them together, not just the single best one. "
    "Your job is to propose which candidate(s) (if any) are the real match, and explain "
    "the variance in plain finance language (e.g. truncated reference, transposed digit, "
    "gateway fee + GST, TDS deduction, unrelated credit, or a batched payout). "
    "You do NOT have authority to close a ledger entry - you are only producing a "
    "recommendation for a human accountant to confirm. "
    "Respond with ONLY a compact JSON object, no prose outside it, in this exact shape: "
    '{"chosen_invoice_ids": ["<invoice_id>", ...] or [], "confidence": <0-100 integer>, '
    '"rationale": "<one or two sentence plain-language explanation>"}'
)


@dataclass
class InvestigatorResult:
    chosen_invoice_ids: list[str] = field(default_factory=list)
    confidence: int = 0
    rationale: str = ""
    source: str = "rule_based_fallback"  # "llm" or "rule_based_fallback"
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 0
    # item 4: which of the 5 possible paths this call took -- logged to the
    # audit trail so "the LLM was flaky but recovered" is visible, not hidden
    # behind a single opaque "rule_based_fallback" label.
    #   "no_llm"                    -- no key / use_llm=False, never attempted
    #   "succeeded_first_try"       -- one clean call, no retry needed
    #   "retried_then_succeeded"    -- transient failure(s), then a real answer
    #   "retried_then_fell_back"    -- transient failure(s), retries exhausted
    #   "failed_first_try_fell_back" -- a non-transient failure, no retry attempted
    llm_path: str = "no_llm"

    @property
    def chosen_invoice_id(self) -> Optional[str]:
        """Backward-compatible single-id accessor -- the first proposed id,
        or None if this investigator declined to propose anything."""
        return self.chosen_invoice_ids[0] if self.chosen_invoice_ids else None


def _rule_based_fallback(settlement: dict, candidates: list[dict]) -> InvestigatorResult:
    """Deterministic, explainable stand-in used when no LLM is available.

    Proposes every candidate that clears the confidence bar, not just the
    single best one -- a settlement can genuinely be one payout covering
    several invoices from the candidate short-list (item 5). Combined
    confidence is the minimum across the proposed set (the whole batch
    proposal is only as strong as its weakest member), never the max.
    """
    if not candidates:
        return InvestigatorResult(
            chosen_invoice_ids=[], confidence=0,
            rationale="No open invoice within the amount/date/reference tolerance window - "
                      "likely an unrelated credit or a settlement for an invoice outside this batch.",
            source="rule_based_fallback",
        )

    qualifying = [c for c in candidates if c["composite_score"] >= CANDIDATE_THRESHOLD]
    if not qualifying:
        top = candidates[0]
        return InvestigatorResult(
            chosen_invoice_ids=[], confidence=int(top["composite_score"]),
            rationale=f"Best candidate ({top['invoice_id']}) only scored {top['composite_score']:.0f}/100 - "
                      "below the confidence bar to recommend automatically. Needs manual review.",
            source="rule_based_fallback",
        )

    def reasons_for(c: dict) -> str:
        bits = []
        if c.get("ref_score", 0) >= 60:
            bits.append(f"reference text is {c['ref_score']:.0f}% similar to {c['invoice_id']}'s code")
        if c.get("amount_score", 0) >= 60:
            bits.append("amount is within a plausible fee/GST/TDS deduction range of the invoice total")
        if c.get("date_score", 0) >= 60:
            bits.append("settlement date falls in the expected post-invoice window")
        return "; ".join(bits) if bits else "it is the closest overall match among open invoices"

    # A batch proposal is only offered when the qualifying candidates'
    # combined remaining_amount actually reconciles against the settlement
    # (same 2%-tolerance discipline the deterministic layers use for
    # combined amounts) -- NOT just because more than one candidate
    # independently scored decently on its own unrelated merits. An earlier
    # version of this function proposed every qualifying candidate as a
    # batch unconditionally; measured against ground truth that cost 5.2pp
    # of precision and 5.2pp of recall (evaluate.py, 5 seeds, n=200) because
    # it was, in effect, guessing a second invoice that merely resembled the
    # settlement without the amounts actually adding up.
    settlement_amount = settlement.get("amount")
    total_remaining = sum(c.get("remaining_amount", 0) for c in qualifying) if len(qualifying) > 1 else 0
    combined_amount_fits = (
        len(qualifying) > 1 and settlement_amount is not None and total_remaining > 0
        and abs(total_remaining - settlement_amount) / total_remaining <= 0.02
    )

    if combined_amount_fits:
        ids = [c["invoice_id"] for c in qualifying]
        combined_confidence = int(min(min(c["composite_score"] for c in qualifying), 89))
        rationale = (f"Rule-based batch proposal: {len(qualifying)} candidates' combined open "
                    f"balance ({total_remaining:.2f}) reconciles against the settlement "
                    f"({settlement_amount:.2f}) -- " + " | ".join(
                        f"{c['invoice_id']}: {reasons_for(c)}" for c in qualifying) + ".")
        return InvestigatorResult(chosen_invoice_ids=ids, confidence=combined_confidence,
                                  rationale=rationale, source="rule_based_fallback")

    # Otherwise: propose only the single best-scoring candidate, exactly the
    # original (pre-batch-feature) behavior -- multiple candidates merely
    # resembling the settlement, with no amount corroboration, is not
    # evidence of a real batch.
    top = qualifying[0]
    return InvestigatorResult(
        chosen_invoice_ids=[top["invoice_id"]],
        confidence=int(min(top["composite_score"], 89)),
        rationale="Rule-based match: " + reasons_for(top) + ".",
        source="rule_based_fallback",
    )


def investigate(settlement: dict, candidates: list[dict], use_llm: bool = True,
                on_retry: Optional[Callable[[int, int, str], None]] = None) -> InvestigatorResult:
    """Investigate one unresolved settlement against its candidate invoices.

    `settlement` = {txn_id, txn_date, amount, narration}
    `candidates` = list of dicts, each already scored by matcher.py's fuzzy
                   layer: {invoice_id, customer, invoice_date, amount,
                   remaining_amount, reference_code, ref_score, amount_score,
                   date_score, composite_score}, sorted best-first (top 3 max).
    `use_llm`   = if False, always use the rule-based fallback (useful for a
                   fully offline/deterministic demo run).
    `on_retry`  = optional callback(attempt, max_attempts, txn_id) invoked
                   right before each retry sleep -- app.py wires this to a
                   "retrying (attempt 2/3)..." UI message (item 4). Never
                   called when use_llm=False or on a non-transient failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not use_llm or not api_key:
        result = _rule_based_fallback(settlement, candidates)
        result.llm_path = "no_llm"
        return result

    last_exc: Optional[Exception] = None
    attempt = 0
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            from google import genai  # imported lazily so the module works with no SDK installed too
            from google.genai import types as genai_types

            client = genai.Client(api_key=api_key)
            user_payload = {
                "settlement": settlement,
                "candidate_invoices": candidates[:3],
            }
            response = client.models.generate_content(
                model=DEFAULT_MODEL,
                contents=json.dumps(user_payload, default=str),
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0,
                    # 2048, not a tight 300: gemini-3.6-flash is a "thinking"
                    # model that spends part of the output budget on internal
                    # reasoning before the visible answer (observed ~500-600
                    # thinking tokens on this prompt) -- a tight budget gets
                    # the response cut off (finish_reason=MAX_TOKENS) before
                    # the JSON is ever written, which reads as a parse
                    # failure, not what actually happened. Found by testing
                    # against the live API, not assumed.
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                ),
            )
            raw_text = response.text or ""
            parsed = _extract_json(raw_text)
            if parsed is None:
                raise ValueError(f"Could not parse JSON from LLM response: {raw_text!r}")

            # Tolerate the old singular-field shape too, in case a cached
            # prompt or a slightly-off model response uses it -- never crash
            # on shape.
            ids = parsed.get("chosen_invoice_ids")
            if ids is None:
                single = parsed.get("chosen_invoice_id")
                ids = [single] if single else []
            valid_ids = {c["invoice_id"] for c in candidates}
            ids = [i for i in ids if i in valid_ids]  # never propose an id we didn't offer

            usage = response.usage_metadata
            # Gemini bills thinking tokens as output too -- folding
            # thoughts_token_count into output_tokens keeps the cost strip
            # honest instead of quietly undercounting real spend.
            visible_out = getattr(usage, "candidates_token_count", 0) or 0
            thinking_out = getattr(usage, "thoughts_token_count", 0) or 0
            return InvestigatorResult(
                chosen_invoice_ids=ids,
                confidence=int(parsed.get("confidence", 0)),
                rationale=str(parsed.get("rationale", "")).strip() or "(no rationale provided)",
                source="llm",
                input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                output_tokens=visible_out + thinking_out,
                attempts=attempt,
                llm_path="succeeded_first_try" if attempt == 1 else "retried_then_succeeded",
            )
        except Exception as exc:  # noqa: BLE001 - any failure must degrade gracefully, never crash the pipeline
            last_exc = exc
            if attempt < RETRY_MAX_ATTEMPTS and _is_transient(exc):
                if on_retry:
                    on_retry(attempt + 1, RETRY_MAX_ATTEMPTS, settlement.get("txn_id", ""))
                time.sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
                continue
            break  # non-transient error, or retries exhausted -- stop trying

    fallback = _rule_based_fallback(settlement, candidates)
    fallback.attempts = attempt
    fallback.llm_path = "retried_then_fell_back" if attempt > 1 else "failed_first_try_fell_back"
    fallback.rationale = (f"[LLM call failed after {attempt} attempt(s) "
                          f"({last_exc.__class__.__name__}), used rule-based fallback] "
                          + fallback.rationale)
    return fallback


def _extract_json(text: str) -> Optional[dict]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None
