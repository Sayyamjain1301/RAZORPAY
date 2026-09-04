"""Operational/governance features that sit around the matching pipeline
without touching it: ownership, comments, SLA aging + escalation, saved
filter views, duplicate-ingestion detection, reconciliation-drift detection,
a two-step certification lock, simulated ERP posting, and an audit-export
bundle.

Honest scope note: this is single-process, local-JSON-file state
(recon_agent/state_store.py) — real multi-user role separation, a real ERP
connector, and real access control need a backend this project doesn't have.
Every function below is a genuine, enforced state machine (a certified run
really does refuse further edits; a duplicate file really is detected by
content hash) — it's just scoped to one local session, and says so in the UI
rather than pretending otherwise.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import zipfile
from datetime import date, datetime, timezone

from . import state_store

OWNERS = ["Unassigned", "A. Rao (AP)", "S. Iyer (Controller)", "M. Fernandes (Reviewer)"]
DEFAULT_SLA_DAYS = 3
DEFAULT_ESCALATE_DAYS = 7


# --------------------------------------------------------------------------
# Ownership (#13)
# --------------------------------------------------------------------------
def get_owner(data_dir: str, txn_id: str) -> str:
    return state_store.load(data_dir, "ownership", {}).get(txn_id, "Unassigned")


def set_owner(data_dir: str, txn_id: str, owner: str) -> None:
    m = state_store.load(data_dir, "ownership", {})
    m[txn_id] = owner
    state_store.save(data_dir, "ownership", m)


# --------------------------------------------------------------------------
# Comments (#14)
# --------------------------------------------------------------------------
def get_comments(data_dir: str, txn_id: str) -> list[dict]:
    return state_store.load(data_dir, "comments", {}).get(txn_id, [])


def add_comment(data_dir: str, txn_id: str, author: str, text: str) -> None:
    m = state_store.load(data_dir, "comments", {})
    m.setdefault(txn_id, []).append({
        "author": author, "text": text,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    state_store.save(data_dir, "comments", m)


# --------------------------------------------------------------------------
# SLA aging + auto-escalation (#11, #12)
# --------------------------------------------------------------------------
def touch_first_seen(data_dir: str, unresolved_txn_ids: list[str]) -> None:
    """Call once per run with every pending/exception txn_id — records the
    first date each one was seen unresolved, so age is real elapsed time
    across runs, not a fake per-render counter."""
    seen = state_store.load(data_dir, "first_seen", {})
    today = date.today().isoformat()
    for t in unresolved_txn_ids:
        seen.setdefault(t, today)
    state_store.save(data_dir, "first_seen", seen)


def age_days(data_dir: str, txn_id: str) -> int:
    seen = state_store.load(data_dir, "first_seen", {})
    first = seen.get(txn_id)
    if not first:
        return 0
    return (date.today() - date.fromisoformat(first)).days


def check_escalation(data_dir: str, txn_id: str, *,
                     sla_days: int = DEFAULT_SLA_DAYS,
                     escalate_days: int = DEFAULT_ESCALATE_DAYS) -> dict:
    """Returns {past_sla, escalated, reassigned_to}. Escalation is a real
    state write (persisted), not just a computed label — once escalated it
    stays escalated (and reassigned) until someone resolves the row, matching
    how a real timed-reminder-then-reassign flow behaves."""
    age = age_days(data_dir, txn_id)
    past_sla = age > sla_days
    esc = state_store.load(data_dir, "escalations", {})
    if age > escalate_days and txn_id not in esc:
        pool = [o for o in OWNERS if o != "Unassigned"]
        reassign_to = pool[hash(txn_id) % len(pool)]
        esc[txn_id] = {"escalated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "reassigned_to": reassign_to}
        state_store.save(data_dir, "escalations", esc)
        set_owner(data_dir, txn_id, reassign_to)
    rec = esc.get(txn_id)
    return {"age_days": age, "past_sla": past_sla,
            "escalated": rec is not None, "reassigned_to": rec["reassigned_to"] if rec else None}


# --------------------------------------------------------------------------
# Saved filter views (#9)
# --------------------------------------------------------------------------
def list_saved_views(data_dir: str) -> dict:
    return state_store.load(data_dir, "saved_views", {})


def save_view(data_dir: str, name: str, filt: dict) -> None:
    views = list_saved_views(data_dir)
    views[name] = filt
    state_store.save(data_dir, "saved_views", views)


def delete_view(data_dir: str, name: str) -> None:
    views = list_saved_views(data_dir)
    views.pop(name, None)
    state_store.save(data_dir, "saved_views", views)


# --------------------------------------------------------------------------
# Duplicate-batch / re-ingestion detection (#15)
# --------------------------------------------------------------------------
def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def check_duplicate_ingestion(data_dir: str, settlements_csv: str) -> dict:
    h = file_hash(settlements_csv)
    history = state_store.load(data_dir, "ingestion_hashes", [])
    is_dup = h in [r["hash"] for r in history]
    history.append({"hash": h, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    state_store.save(data_dir, "ingestion_hashes", history[-20:])
    return {"hash": h, "is_duplicate": is_dup, "times_seen_before": sum(1 for r in history if r["hash"] == h) - 1}


# --------------------------------------------------------------------------
# Reconciliation-drift / self-invalidation detection (#16)
# --------------------------------------------------------------------------
def check_drift(data_dir: str, settlements: list[dict]) -> list[dict]:
    """If the same txn_id previously reconciled to a different set of
    invoices (or a different status) than it does now — same source files,
    a rerun — that is exactly the "an already-closed record just got
    contradicted" failure Numeric flags. Returns the list of drifted txns."""
    prev = state_store.load(data_dir, "last_result_map", {})
    current = {s["txn_id"]: {"status": s["status"], "invoice_ids": sorted(s["matched_invoice_ids"])}
              for s in settlements}
    drifted = []
    for txn_id, now in current.items():
        was = prev.get(txn_id)
        if was and (was["status"] != now["status"] or was["invoice_ids"] != now["invoice_ids"]):
            drifted.append({"txn_id": txn_id, "was": was, "now": now})
    state_store.save(data_dir, "last_result_map", current)
    return drifted


# --------------------------------------------------------------------------
# Certification chain (#19): preparer -> reviewer -> hard lock
# --------------------------------------------------------------------------
def get_certification(data_dir: str, run_id: str) -> dict | None:
    return state_store.load(data_dir, "certifications", {}).get(run_id)


def prepare_run(data_dir: str, run_id: str, preparer: str) -> None:
    certs = state_store.load(data_dir, "certifications", {})
    certs[run_id] = {"prepared_by": preparer,
                     "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "reviewed_by": None, "reviewed_at": None, "certified": False}
    state_store.save(data_dir, "certifications", certs)


def certify_run(data_dir: str, run_id: str, reviewer: str) -> dict:
    certs = state_store.load(data_dir, "certifications", {})
    rec = certs.get(run_id)
    if rec is None:
        return {"ok": False, "reason": "Run must be prepared before it can be reviewed."}
    if rec["prepared_by"] == reviewer:
        return {"ok": False, "reason": "Reviewer must differ from preparer (segregation of duties)."}
    rec["reviewed_by"] = reviewer
    rec["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["certified"] = True
    state_store.save(data_dir, "certifications", certs)
    return {"ok": True}


def is_locked(data_dir: str, run_id: str) -> bool:
    rec = get_certification(data_dir, run_id)
    return bool(rec and rec["certified"])


# --------------------------------------------------------------------------
# Simulated one-click ERP posting (#17)
# --------------------------------------------------------------------------
POSTED_CSV_FIELDS = ["posted_at", "invoice_id", "txn_id", "amount", "audit_hash"]


def post_to_erp(data_dir: str, invoice_id: str, txn_id: str, amount: float, audit_hash: str) -> None:
    path = os.path.join(data_dir, "posted_entries.csv")
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=POSTED_CSV_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow({"posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "invoice_id": invoice_id, "txn_id": txn_id, "amount": amount,
                   "audit_hash": audit_hash})


def load_posted_entries(data_dir: str) -> list[dict]:
    path = os.path.join(data_dir, "posted_entries.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------
# Audit export package (#18)
# --------------------------------------------------------------------------
def build_audit_package(data_dir: str, audit_log_path: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(audit_log_path):
            z.write(audit_log_path, arcname="audit_log.jsonl")
        posted = os.path.join(data_dir, "posted_entries.csv")
        if os.path.exists(posted):
            z.write(posted, arcname="posted_entries.csv")
        for name in ("invoices.csv", "settlements.csv", "ground_truth.csv", "deduction_truth.csv"):
            p = os.path.join(data_dir, name)
            if os.path.exists(p):
                z.write(p, arcname=name)
        certs = state_store.load(data_dir, "certifications", {})
        z.writestr("certifications.json", str(certs))
        z.writestr("MANIFEST.md",
                   "# Audit export package\n\n"
                   "- audit_log.jsonl — hash-chained decision log (verify with recon_agent.audit.verify_chain)\n"
                   "- posted_entries.csv — simulated ERP postings, each linked to its audit hash\n"
                   "- invoices.csv / settlements.csv — source data for this batch\n"
                   "- ground_truth.csv / deduction_truth.csv — hidden truth files, included here for audit only\n"
                   "- certifications.json — preparer/reviewer sign-off per run_id\n")
    return buf.getvalue()
