"""Multi-seed evaluation harness. Never trust a single run.

This lineage's matcher.py doesn't carry a closed-vocabulary reason_code field
(that's a PRD/v2-only concept, archived at _v2_backup/) — its exceptions
carry a `layer` (always "llm_investigator" for anything that reaches L5) plus
a free-text `rationale`. So the exception-category breakdown here groups by a
short derived label instead of a reason_code, and the same integrity rule
the PRD demands still applies: every category count must sum to exactly
metrics["exception"], enforced with a real assertion, not just eyeballed.

Usage:
    python evaluate.py --seeds 1 2 3 4 5 --n 200 --out eval_out
    python evaluate.py --seeds 1 2 3 4 5 --n 200 --use-llm   # needs a real key
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from collections import Counter

from data_gen import generate
from recon_agent.matcher import ALL_LAYERS, compute_metrics, load_deduction_truth, \
    load_ground_truth, load_invoices, load_settlements, reconcile


def _exception_category(s: dict) -> str:
    """Derived, closed-ish categorization from what this lineage actually
    records (layer + rationale), not a fabricated taxonomy field."""
    if s["layer"] == "none":
        return "ABLATION_LAYER_DISABLED"
    r = (s.get("rationale") or "").lower()
    if "below the confidence bar" in r or "only scored" in r:
        return "LOW_CONFIDENCE_CANDIDATE"
    if "no open invoice" in r:
        return "UNEXPLAINED_CREDIT"
    return "MODEL_COULD_NOT_DECIDE"


def run_once(data_dir: str, *, use_llm: bool, enabled_layers=ALL_LAYERS) -> dict:
    invoices = load_invoices(f"{data_dir}/invoices.csv")
    settlements = load_settlements(f"{data_dir}/settlements.csv")
    gt_path, ded_path = f"{data_dir}/ground_truth.csv", f"{data_dir}/deduction_truth.csv"
    ground_truth = load_ground_truth(gt_path) if os.path.exists(gt_path) else None
    deduction_truth = load_deduction_truth(ded_path) if os.path.exists(ded_path) else None

    t0 = time.perf_counter()
    results = reconcile(invoices, settlements, use_llm=use_llm, enabled_layers=enabled_layers)
    wall_s = time.perf_counter() - t0

    metrics = compute_metrics(results, ground_truth, deduction_truth)

    # ---- Phase 0 item 1: exception categories must sum to the total -------
    exceptions = [s for s in results if s["status"] == "exception"]
    reason_codes = Counter(_exception_category(s) for s in exceptions)
    assert sum(reason_codes.values()) == metrics["exception"] == len(exceptions), (
        f"exception category counts ({sum(reason_codes.values())}) must equal "
        f"metrics['exception'] ({metrics['exception']}) -- got a mismatch, this "
        f"would make scorecard.md's per-category breakdown lie about its own total"
    )

    llm_rows = [s for s in results if s["source"] == "llm"]
    in_tok = sum(s.get("input_tokens", 0) for s in results)
    out_tok = sum(s.get("output_tokens", 0) for s in results)

    # ---- honeypots (item 9): did the agent take the bait? -----------------
    hp_path = f"{data_dir}/honeypots.csv"
    honeypot_ids = load_honeypots(hp_path) if os.path.exists(hp_path) else set()
    results_by_txn = {s["txn_id"]: s for s in results}
    baited = [t for t in honeypot_ids
             if results_by_txn.get(t, {}).get("status") in ("matched", "pending_confirmation")
             and results_by_txn[t]["matched_invoice_ids"]]

    return {
        "n": len(results), "wall_clock_s": round(wall_s, 4),
        "records_per_sec": round(len(results) / wall_s, 2) if wall_s > 0 else None,
        **metrics,
        "reason_codes": dict(reason_codes),
        "cost_split": {
            "deterministic_pct": round((len(results) - len(llm_rows)) / len(results) * 100, 1) if results else 0.0,
            "llm_pct": round(len(llm_rows) / len(results) * 100, 1) if results else 0.0,
            "n_llm_calls": len(llm_rows), "input_tokens": in_tok, "output_tokens": out_tok,
        },
        "honeypots": {"total": len(honeypot_ids), "baited": len(baited), "baited_ids": baited},
    }


def load_honeypots(path: str) -> set[str]:
    import csv
    with open(path, newline="") as f:
        return {row["txn_id"] for row in csv.DictReader(f)}


def multi_seed(base_dir: str, seeds: list[int], n: int, *, use_llm: bool) -> dict:
    runs = []
    for seed in seeds:
        d = os.path.join(base_dir, f"seed_{seed}")
        generate(n, seed, d)
        runs.append(run_once(d, use_llm=use_llm))

    def mean_std(key):
        vals = [r[key] for r in runs if r.get(key) is not None]
        if not vals:
            return None, None
        m = statistics.mean(vals)
        s = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        return round(m, 4), round(s, 4)

    summary = {"seeds": seeds, "n_per_seed": n, "runs": runs}
    for key in ["auto_match_rate", "resolved_rate", "precision", "recall",
               "deduction_hypothesis_accuracy", "records_per_sec"]:
        m, s = mean_std(key)
        summary[key] = {"mean": m, "std": s}
    summary["honeypots"] = {
        "total": sum(r["honeypots"]["total"] for r in runs),
        "baited": sum(r["honeypots"]["baited"] for r in runs),
    }
    return summary


def ablation(data_dir: str, *, use_llm: bool) -> dict:
    """Disable one layer at a time; report each layer's marginal contribution
    to match rate. A layer that contributes ~0 is reported, not hidden."""
    rows = {}
    full = run_once(data_dir, use_llm=use_llm, enabled_layers=ALL_LAYERS)
    rows["all_layers"] = full
    for layer in sorted(ALL_LAYERS):
        subset = ALL_LAYERS - {layer}
        rows[f"without_{layer}"] = run_once(data_dir, use_llm=use_llm, enabled_layers=subset)
    return rows


def decision_determinism(data_dir: str, *, use_llm: bool, reps: int = 5) -> float:
    """Rerun the same batch N times on identical input; report the fraction
    of txn_ids whose final (status, matched_invoice_ids) is identical every
    time. A live LLM call is not seeded, so DecDet on a use_llm=True run
    honestly measures the model's own consistency, not a bug if it's <100%."""
    runs = []
    for _ in range(reps):
        invoices = load_invoices(f"{data_dir}/invoices.csv")
        settlements = load_settlements(f"{data_dir}/settlements.csv")
        runs.append(reconcile(invoices, settlements, use_llm=use_llm))

    by_txn: dict[str, set] = {}
    for run in runs:
        for s in run:
            key = (s["status"], tuple(sorted(s["matched_invoice_ids"])))
            by_txn.setdefault(s["txn_id"], set()).add(key)
    stable = sum(1 for v in by_txn.values() if len(v) == 1)
    return round(stable / len(by_txn), 4) if by_txn else 1.0


def scorecard_markdown(summary: dict) -> str:
    ms = summary["multi_seed"]
    demo = summary["demo_batch"]
    abl = summary["ablation"]
    dd = summary["decision_determinism"]
    ct = demo["cost_split"]
    lines = [
        "```",
        f"BATCH  n={ms['n_per_seed']}, {len(ms['seeds'])} seeds, frozen generator  "
        f"[demo batch shown live: n={demo['n']}]",
        "",
        f"THROUGHPUT   records/sec .... {ms['records_per_sec']['mean']} +/- {ms['records_per_sec']['std']}"
        f"      wall-clock (demo) ... {demo['wall_clock_s']}s",
        f"             deterministic share ..... {ct['deterministic_pct']}%   llm calls ... {ct['n_llm_calls']}",
        "",
        f"ACCURACY (mean +/- std across {len(ms['seeds'])} seeds)",
        f"             auto-match rate ......... {ms['auto_match_rate']['mean']} +/- {ms['auto_match_rate']['std']}",
        f"             precision ............... {ms['precision']['mean']} +/- {ms['precision']['std']}",
        f"             recall .................. {ms['recall']['mean']} +/- {ms['recall']['std']}",
        f"             dedup-hypothesis acc ..... {ms['deduction_hypothesis_accuracy']['mean']} "
        f"+/- {ms['deduction_hypothesis_accuracy']['std']}",
        f"             DecDet ({dd['reps']} identical reruns) ....... {dd['value']*100:.1f}%",
        f"             honeypots baited ......... {ms['honeypots']['baited']} of {ms['honeypots']['total']} "
        f"(across all {len(ms['seeds'])} seeds)",
        "",
        "ABLATION (demo batch, marginal contribution to match rate)",
    ]
    for key, row in abl.items():
        lines.append(f"             {key:<22} auto_match_rate={row['auto_match_rate']}")
    lines += [
        "",
        f"EXCEPTIONS   {demo['exception']} records — categories: "
        + ", ".join(f"{k}={v}" for k, v in demo["reason_codes"].items()),
        f"             matched {demo['matched']} + pending {demo['pending_confirmation']} + "
        f"exceptions {demo['exception']} = {demo['total_settlements']} "
        f"(checks: {demo['matched']+demo['pending_confirmation']+demo['exception']==demo['total_settlements']})",
        "```",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="eval_out")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--demo-n", type=int, default=60)
    ap.add_argument("--demo-seed", type=int, default=42)
    ap.add_argument("--use-llm", action="store_true",
                    help="Use a live Claude call for L5 (needs ANTHROPIC_API_KEY). "
                         "Without this flag, every run uses the deterministic rule-based "
                         "fallback -- honestly labelled in cost_split, not hidden.")
    ap.add_argument("--decdet-reps", type=int, default=5)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)

    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if a.use_llm and not api_key_present:
        print("WARNING: --use-llm was passed but ANTHROPIC_API_KEY is not set. "
             "Every L5 call will silently use the rule-based fallback instead -- "
             "this run will NOT reflect real LLM cost or behavior.")

    print(f"Multi-seed run: seeds={a.seeds} n={a.n} use_llm={a.use_llm} "
         f"(api_key_present={api_key_present}) ...")
    ms = multi_seed(a.out, a.seeds, a.n, use_llm=a.use_llm)

    print("Demo batch ...")
    demo_dir = os.path.join(a.out, "demo")
    generate(a.demo_n, a.demo_seed, demo_dir)
    demo = run_once(demo_dir, use_llm=a.use_llm)

    print("Ablation (on the demo batch) ...")
    abl = ablation(demo_dir, use_llm=a.use_llm)

    print(f"Decision determinism ({a.decdet_reps} reruns) ...")
    dd_value = decision_determinism(demo_dir, use_llm=a.use_llm, reps=a.decdet_reps)

    summary = {
        "api_key_present": api_key_present, "use_llm_requested": a.use_llm,
        "multi_seed": ms, "demo_batch": demo, "ablation": abl,
        "decision_determinism": {"reps": a.decdet_reps, "value": dd_value},
    }
    with open(os.path.join(a.out, "metrics.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)

    card = scorecard_markdown(summary)
    with open(os.path.join(a.out, "scorecard.md"), "w") as f:
        f.write(card)
    print(card)
    print(f"\nWrote {a.out}/metrics.json and {a.out}/scorecard.md")


if __name__ == "__main__":
    main()
