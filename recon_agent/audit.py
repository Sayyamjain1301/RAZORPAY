"""Append-only, hash-chained JSONL audit log + deterministic replay.

One record per settlement decision. Each record's hash covers its own
content plus the previous record's hash, so any edit after the fact breaks
every hash downstream of it — `verify_chain()` catches that in one pass.

Feature #20 ("replay link on every row"): `replay_txn()` re-runs the
deterministic pipeline from the same two source CSVs and checks whether one
specific txn_id reproduces the same decision. Deterministic layers (1-4)
should always match; an `llm_investigator` decision sourced from a live model
call is honestly reported as non-replayable (a rule-based fallback decision
*is* replayable, and is checked the same way).
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

GENESIS_HASH = "0" * 64


def _hash(record: dict) -> str:
    body = json.dumps(record, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


class AuditLog:
    def __init__(self, path: str, *, run_id: str | None = None,
                user: str = "local", model: str = "none", agent_version: str = "1.0.0"):
        self.path = path
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:20]}"
        self.user = user
        self.model = model
        self.agent_version = agent_version
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Continue the existing chain across runs — a fresh instance must
        # not reset to genesis if the file already has records, or every
        # run after the first would look tampered to verify_chain().
        existing = load_chain(path)
        self.prev_hash = existing[-1]["hash"] if existing else GENESIS_HASH

    def record(self, settlement: dict) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "decision_id": f"dec_{uuid.uuid4().hex[:20]}",
            "run_id": self.run_id,
            "user": self.user,
            "agent_version": self.agent_version,
            "model": self.model if settlement.get("source") == "llm" else "none (deterministic/rule)",
            "inputs": {"txn_id": settlement["txn_id"]},
            "rule_invoked": settlement["layer"],
            "reasoning": settlement["rationale"],
            "output": {"status": settlement["status"],
                      "invoice_ids": settlement["matched_invoice_ids"],
                      "confidence": settlement["confidence"]},
            "action": f"invoices {settlement['matched_invoice_ids']} updated"
                      if settlement["status"] == "matched" else "none — proposal or exception only",
            "review": {"by": None, "at": None},
            # item 4: which of the possible LLM-call paths this decision took
            # -- "the model was flaky but recovered" is now visible in the
            # audit trail, not silently collapsed into one fallback label.
            "llm_attempts": settlement.get("llm_attempts", 0),
            "llm_path": settlement.get("llm_path", "n/a"),
        }
        entry["prev_hash"] = self.prev_hash
        entry["hash"] = _hash(entry)
        self.prev_hash = entry["hash"]
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def write_all(self, settlements: list[dict]) -> None:
        for s in settlements:
            self.record(s)


def load_chain(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def verify_chain(path: str) -> tuple[bool, str]:
    entries = load_chain(path)
    prev = GENESIS_HASH
    for i, e in enumerate(entries):
        if e["prev_hash"] != prev:
            return False, f"record {i} ({e['decision_id']}): prev_hash does not chain"
        check = dict(e)
        del check["hash"]
        if _hash(check) != e["hash"]:
            return False, f"record {i} ({e['decision_id']}): hash mismatch — tampered"
        prev = e["hash"]
    return True, f"{len(entries)} records verified, chain intact"


def entries_for_txn(path: str, txn_id: str) -> list[dict]:
    return [e for e in load_chain(path) if e["inputs"].get("txn_id") == txn_id]


def replay_txn(txn_id: str, invoices_csv: str, settlements_csv: str, *,
              use_llm: bool, logged_entry: dict) -> dict:
    """Re-run the whole deterministic pipeline from the two source CSVs and
    check whether this one txn_id reproduces the logged decision.

    Deterministic layers always should. An `llm_investigator` decision that
    came from a *live* model call cannot be replayed byte-for-byte (the model
    call itself is not seeded) — that's reported honestly, not silently
    passed. A rule-based-fallback decision is fully deterministic and is
    checked the same as any other layer.
    """
    from .matcher import load_invoices, load_settlements, reconcile

    invoices = load_invoices(invoices_csv)
    settlements = load_settlements(settlements_csv)
    results = reconcile(invoices, settlements, use_llm=use_llm)
    rerun = next((r for r in results if r["txn_id"] == txn_id), None)
    if rerun is None:
        return {"replayable": False, "reason": "txn_id not found in current source data "
                "(source CSVs were regenerated since this decision was logged)"}

    was_live_llm = logged_entry["model"] not in (None, "none", "none (deterministic/rule)")
    matches = (rerun["status"] == logged_entry["output"]["status"]
              and sorted(rerun["matched_invoice_ids"]) == sorted(logged_entry["output"]["invoice_ids"]))

    if was_live_llm:
        return {"replayable": "not_applicable", "matches": matches,
                "reason": "This decision came from a live LLM call, which is not seeded — "
                          "exact replay cannot be guaranteed by design. Recomputed anyway "
                          f"for reference: {'matched' if matches else 'DID NOT match'} the logged output."}
    return {"replayable": True, "matches": matches,
            "reason": "Deterministic/rule-based decision — recomputing from the same source "
                      f"CSVs {'reproduced' if matches else 'DID NOT reproduce'} the logged output."}
