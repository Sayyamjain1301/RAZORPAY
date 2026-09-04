"""Quick CLI runner - useful for testing and for a terminal-only demo fallback.

Usage: python run_cli.py [--no-llm]
"""
import argparse
import json
import os

from recon_agent.matcher import run_reconciliation

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true", help="force rule-based fallback, skip any API call")
    parser.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "data"))
    args = parser.parse_args()

    d = args.data_dir
    out = run_reconciliation(
        invoices_csv=os.path.join(d, "invoices.csv"),
        settlements_csv=os.path.join(d, "settlements.csv"),
        ground_truth_csv=os.path.join(d, "ground_truth.csv"),
        deduction_truth_csv=os.path.join(d, "deduction_truth.csv"),
        use_llm=not args.no_llm,
    )

    print(json.dumps(out["metrics"], indent=2))

    print("\nBy layer:")
    from collections import Counter
    layer_counts = Counter(s["layer"] for s in out["settlements"])
    for layer, count in layer_counts.most_common():
        print(f"  {layer}: {count}")

    print("\nException / pending samples:")
    shown = 0
    for s in out["settlements"]:
        if s["status"] in ("pending_confirmation", "exception") and shown < 8:
            print(f"  [{s['status']}] {s['txn_id']} -> {s['matched_invoice_ids']} "
                  f"(conf={s['confidence']}, source={s.get('source')}) :: {s['rationale']}")
            shown += 1
