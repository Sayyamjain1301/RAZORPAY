"""Rule model: autonomy per resolution category, dry-run backtesting, and
rule-effectiveness tracking.

Feature #1 (rule preview/dry-run), #2 (Automatic vs Suggested per rule),
#3 (confidence threshold -> coverage/precision curve), #4 (autonomy dial with
auto-promotion), #5 (rule-effectiveness dashboard).

Honest scope note: the deterministic layers (exact-reference, deduction
engine, anchored batch, subset-sum) already auto-close with a mathematical
proof attached — there is no "suggest-only" mode for them to toggle, because
turning that off would mean deliberately holding back a provable match for no
reason. The one category with a real autonomy lever is `llm_investigator`
(layer 5): its raw output is always a proposal, and the rule below controls
whether a human must click Confirm, or whether the agent may auto-confirm
proposals that clear a confidence bar. Every other "layer rule" exists so the
effectiveness dashboard has one row per category, and is fixed at
auto_and_post (fires as part of the deterministic core, can't be demoted).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import state_store

AUTONOMY_LEVELS = ["suggest_only", "auto_under_threshold", "auto_and_post"]
AUTONOMY_LABEL = {
    "suggest_only": "Suggest only — human confirms every one",
    "auto_under_threshold": "Auto-confirm under a confidence floor",
    "auto_and_post": "Auto-confirm + auto-post (no human gate)",
}

DETERMINISTIC_LAYERS = [
    "exact_reference+deduction_engine", "exact_reference+partial_payment",
    "exact_reference_batch+deduction_engine", "anchored_batch_completion",
    "amount_date_subset_sum",
]
TUNABLE_LAYER = "llm_investigator"

PROMOTION_STREAK = 5   # consecutive accepted, zero-override suggestions before we offer to promote


@dataclass
class Rule:
    layer: str
    label: str
    autonomy: str = "auto_and_post"
    threshold: int = 85
    tunable: bool = False

    def to_dict(self):
        return asdict(self)


def default_rules() -> dict[str, Rule]:
    rules = {}
    for layer in DETERMINISTIC_LAYERS:
        rules[layer] = Rule(layer=layer, label=layer.replace("_", " ").replace("+", " + "),
                            autonomy="auto_and_post", tunable=False)
    rules[TUNABLE_LAYER] = Rule(layer=TUNABLE_LAYER, label="Tier-1 Investigator proposals",
                                autonomy="suggest_only", threshold=85, tunable=True)
    return rules


def load_rules(data_dir: str) -> dict[str, Rule]:
    raw = state_store.load(data_dir, "rules", None)
    if not raw:
        rules = default_rules()
        save_rules(data_dir, rules)
        return rules
    return {k: Rule(**v) for k, v in raw.items()}


def save_rules(data_dir: str, rules: dict[str, Rule]) -> None:
    state_store.save(data_dir, "rules", {k: r.to_dict() for k, r in rules.items()})


def apply_autonomy(settlements: list[dict], rules: dict[str, Rule]) -> dict[str, str]:
    """Returns {txn_id: rule_label} for every pending_confirmation item that
    the active rule set auto-confirms without a human click. Never touches
    `exception` rows — a rule can only promote a proposal, never invent one."""
    auto_confirmed = {}
    for s in settlements:
        if s["status"] != "pending_confirmation":
            continue
        rule = rules.get(s["layer"])
        if rule is None or rule.autonomy == "suggest_only":
            continue
        if rule.autonomy == "auto_and_post" or (
                rule.autonomy == "auto_under_threshold" and s["confidence"] >= rule.threshold):
            auto_confirmed[s["txn_id"]] = rule.label
    return auto_confirmed


def dry_run(rule: Rule, settlements: list[dict], ground_truth: dict[str, set]) -> dict:
    """Backtest: if this rule's current autonomy/threshold were applied to
    this batch's pending_confirmation items, how many would be right vs wrong?
    This is the exact feature the brief flags as the strongest anti-black-box
    signal in the market (Modern Treasury's rule preview)."""
    candidates = [s for s in settlements if s["layer"] == rule.layer
                 and s["status"] == "pending_confirmation"]
    if rule.autonomy == "suggest_only":
        would_auto = []
    elif rule.autonomy == "auto_and_post":
        would_auto = candidates
    else:
        would_auto = [s for s in candidates if s["confidence"] >= rule.threshold]

    would_match = would_mismatch = 0
    for s in would_auto:
        predicted = set(s["matched_invoice_ids"])
        true = ground_truth.get(s["txn_id"], set())
        if predicted == true and predicted:
            would_match += 1
        else:
            would_mismatch += 1

    return {
        "layer": rule.layer, "candidates": len(candidates),
        "would_auto_close": len(would_auto),
        "would_match": would_match, "would_mismatch": would_mismatch,
        "would_be_precision": round(would_match / len(would_auto), 4) if would_auto else None,
        "left_for_review": len(candidates) - len(would_auto),
    }


def threshold_sweep(settlements: list[dict], ground_truth: dict[str, set],
                    layer: str = TUNABLE_LAYER, step: int = 5) -> list[dict]:
    """Coverage/precision at every confidence threshold, for the live-wired
    slider (#3) and the risk-coverage-style chart in Reports."""
    pool = [s for s in settlements if s["layer"] == layer and s["status"] in
            ("pending_confirmation", "matched")]
    n = len(pool) or 1
    out = []
    for tau in range(0, 101, step):
        accepted = [s for s in pool if s["confidence"] >= tau]
        if not accepted:
            out.append({"tau": tau, "coverage": 0.0, "precision": 1.0, "n": 0})
            continue
        correct = sum(1 for s in accepted
                      if set(s["matched_invoice_ids"]) == ground_truth.get(s["txn_id"], set())
                      and s["matched_invoice_ids"])
        out.append({"tau": tau, "coverage": round(len(accepted) / n, 4),
                    "precision": round(correct / len(accepted), 4), "n": len(accepted)})
    return out


# --------------------------------------------------------------------------
# Rule-effectiveness dashboard (#5) — needs run history + human overrides
# --------------------------------------------------------------------------

def record_run(data_dir: str, metrics: dict, settlements: list[dict]) -> None:
    history = state_store.load(data_dir, "run_history", [])
    from collections import Counter
    layer_counts = dict(Counter(s["layer"] for s in settlements))
    history.append({
        "auto_match_rate": metrics.get("auto_match_rate"),
        "resolved_rate": metrics.get("resolved_rate"),
        "precision": metrics.get("precision"),
        "recall": metrics.get("recall"),
        "deduction_hypothesis_accuracy": metrics.get("deduction_hypothesis_accuracy"),
        "total_settlements": metrics.get("total_settlements"),
        "layer_counts": layer_counts,
    })
    state_store.save(data_dir, "run_history", history[-50:])   # keep last 50 runs


def load_run_history(data_dir: str) -> list[dict]:
    return state_store.load(data_dir, "run_history", [])


def record_override(data_dir: str, layer: str, accepted: bool) -> None:
    """Every human Confirm (accepted=True) or Reject (accepted=False) on a
    pending_confirmation row feeds the per-rule override count and the
    consecutive-accept streak used for autonomy promotion suggestions."""
    stats = state_store.load(data_dir, "rule_stats", {})
    s = stats.setdefault(layer, {"accepted": 0, "overridden": 0, "streak": 0})
    if accepted:
        s["accepted"] += 1
        s["streak"] += 1
    else:
        s["overridden"] += 1
        s["streak"] = 0
    state_store.save(data_dir, "rule_stats", stats)


def load_rule_stats(data_dir: str) -> dict:
    return state_store.load(data_dir, "rule_stats", {})


def promotion_candidates(data_dir: str, rules: dict[str, Rule]) -> list[str]:
    """Layers whose accept streak has cleared PROMOTION_STREAK with zero
    overrides in that streak — Vic.ai's graduated-trust pattern (#4)."""
    stats = load_rule_stats(data_dir)
    out = []
    for layer, rule in rules.items():
        if not rule.tunable or rule.autonomy == "auto_and_post":
            continue
        st = stats.get(layer, {})
        if st.get("streak", 0) >= PROMOTION_STREAK:
            out.append(layer)
    return out
