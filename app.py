"""
Streamlit console for the Payment Reconciliation Agent.

Visual system: Razorpay's brand palette + a restrained motion layer where
every animation communicates a state change (recon_agent/{ui_theme,motion}.py
— see motion.py's module docstring for how each animation is implemented
within Streamlit's rerun model, and why). The matching pipeline (data_gen.py,
recon_agent/matcher.py, recon_agent/llm_reasoner.py) is unchanged.

Run:  streamlit run app.py
"""
import os
import time
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from data_gen import generate
from recon_agent import (anomaly, assistant, chat_widget, insights, motion, ops,
                         rules as rules_mod)
from recon_agent.audit import AuditLog, entries_for_txn, load_chain, replay_txn, verify_chain
from recon_agent.matcher import load_settlements, run_reconciliation
from recon_agent.ui_theme import (CSS, STATUS_LABEL, autonomy_badge, band_for, band_row,
                                  because_sentence, confidence_pill, fmt_inr,
                                  fmt_inr_compact, icon, mini_pipeline, sparkline_svg,
                                  status_row, tag, trend_arrow)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INV_CSV = os.path.join(DATA_DIR, "invoices.csv")
SETTLE_CSV = os.path.join(DATA_DIR, "settlements.csv")
GT_CSV = os.path.join(DATA_DIR, "ground_truth.csv")
DED_CSV = os.path.join(DATA_DIR, "deduction_truth.csv")
AUDIT_PATH = os.path.join(DATA_DIR, "audit_log.jsonl")

CURRENT_USER = os.environ.get("USER", "local_user")
LAYER_LABEL = {
    "exact_reference+deduction_engine": "Exact reference match",
    "exact_reference+partial_payment": "Exact reference match",
    "exact_reference_batch+deduction_engine": "Exact reference match (batch)",
    "anchored_batch_completion": "Batch reconciliation",
    "amount_date_subset_sum": "Batch reconciliation",
    "llm_investigator": "Exception investigator",
}

st.set_page_config(page_title="Reconciliation — Razorpay", layout="wide",
                    page_icon=":material/receipt_long:", initial_sidebar_state="expanded")
st.markdown(CSS, unsafe_allow_html=True)
st.markdown(motion.CSS, unsafe_allow_html=True)

# ---- st.secrets fallback for GEMINI_API_KEY: env var always wins ----------
# recon_agent/{llm_reasoner,assistant}.py both read os.environ directly, so
# rather than plumb a second lookup path through every module, we bridge
# st.secrets into the environment once, here, only when the env var is
# genuinely absent — every downstream `os.environ.get(...)` then just works,
# and precedence (env > secrets) is enforced by only ever filling a gap,
# never overwriting. `st.secrets` raises if no secrets.toml exists at all,
# which is the normal case for most local dev — that's expected, not a bug.
for _key_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
    if not os.environ.get(_key_name):
        try:
            _secret_key = st.secrets.get(_key_name)
            if _secret_key:
                os.environ[_key_name] = _secret_key
        except Exception:
            pass  # no secrets.toml configured — fall through to the no-key path

st.session_state.setdefault("dark_mode", False)
if st.session_state["dark_mode"]:
    # Dark mode is Prussian Blue itself as the base (see its "dark-mode
    # base" comment in ui_theme.py), with everything else a White-at-
    # various-opacity derivation on top -- no separate near-black gray
    # scale invented for it, so it stays strictly Razorpay Blue + White.
    st.markdown("""<style>
        [data-testid="stAppViewContainer"] { background: #012652 !important; }
        [data-testid="stSidebar"] { background: #012652 !important;
            border-right: 1px solid rgba(255,255,255,0.12) !important; }
        .rp-card { background: rgba(255,255,255,0.06) !important; border-color: rgba(255,255,255,0.15) !important; }
        body, p, span, div, label, li { color: rgba(255,255,255,0.92); }
        [data-testid="stMetricLabel"] { color: rgba(255,255,255,0.55) !important; }
    </style>""", unsafe_allow_html=True)

for key, default in [
    ("confirmed", set()), ("rejected", set()), ("selected_txn", None),
    ("run_id", None), ("bulk_selected", set()), ("status_filter", []),
    ("band_filter", []), ("layer_filter", []), ("search_text", ""),
    ("last_panel_txn", None), ("seen_txn_ids", set()), ("kpi_animated_runs", set()),
    ("confirm_anim", None), ("active_tab", "Overview"),
]:
    st.session_state.setdefault(key, default)


def enrich_settlements(settlements: list[dict], settle_csv_path: str) -> dict[str, dict]:
    raw = {r["txn_id"]: r for r in load_settlements(settle_csv_path)}
    enriched = {}
    for s in settlements:
        r = raw.get(s["txn_id"], {})
        narration = r.get("narration", "")
        tokens = [t for t in narration.replace("/", " ").replace("-", " ").split() if t.isalpha()]
        guess = tokens[-1].title() if tokens else None
        enriched[s["txn_id"]] = {
            "txn_date": r.get("txn_date"), "amount": r.get("amount"),
            "narration": narration, "narration_customer_guess": guess,
        }
    return enriched


def ticker_lines(metrics: dict, settlements: list[dict]) -> list[str]:
    """Real per-layer counts from the run that just finished (#5) — never
    fabricated. Grouped into the same three buckets the README's architecture
    diagram uses, so the ticker teaches the real pipeline shape."""
    from collections import Counter
    counts = Counter(s["layer"] for s in settlements)
    exact = (counts.get("exact_reference+deduction_engine", 0)
            + counts.get("exact_reference+partial_payment", 0)
            + counts.get("exact_reference_batch+deduction_engine", 0))
    batch = counts.get("anchored_batch_completion", 0) + counts.get("amount_date_subset_sum", 0)
    investigator = counts.get("llm_investigator", 0)
    lines = ["Normalizing records…"]
    if exact:
        lines.append(f"Exact reference match — {exact} resolved")
    if batch:
        lines.append(f"Batch reconciliation — {batch} resolved")
    if investigator:
        lines.append(f"Exception investigator — {investigator} flagged")
    return lines


def run_pipeline_once(use_llm: bool, retry_placeholder) -> tuple[bool, str | None]:
    """Runs the reconciliation pipeline once and updates every piece of
    session state a completed run needs — shared by the sidebar's Run
    button and Take the tour so the two paths can't silently drift apart.
    Returns (ok, exception_class_name_or_None); on failure the full
    traceback is still logged to data/error.log, same global error
    boundary as before this was extracted into a function."""
    def _on_llm_retry(attempt: int, max_attempts: int, txn_id: str) -> None:
        retry_placeholder.info(f"Retrying LLM call for {txn_id} (attempt {attempt}/{max_attempts})…")

    try:
        dup = ops.check_duplicate_ingestion(DATA_DIR, SETTLE_CSV) if os.path.exists(SETTLE_CSV) else None
        _t0 = time.perf_counter()
        out = run_reconciliation(
            invoices_csv=INV_CSV, settlements_csv=SETTLE_CSV,
            ground_truth_csv=GT_CSV if os.path.exists(GT_CSV) else None,
            deduction_truth_csv=DED_CSV if os.path.exists(DED_CSV) else None,
            use_llm=use_llm, on_llm_retry=_on_llm_retry,
        )
        retry_placeholder.empty()
        st.session_state["wall_clock_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        st.session_state["run_id"] = run_id
        st.session_state["recon_out"] = out
        st.session_state["confirmed"] = set()
        st.session_state["rejected"] = set()
        st.session_state["dup_check"] = dup
        st.session_state["enriched"] = enrich_settlements(out["settlements"], SETTLE_CSV)
        st.session_state["seen_txn_ids"] = set()

        audit = AuditLog(AUDIT_PATH, run_id=run_id, user=CURRENT_USER,
                         model=os.environ.get("RECON_LLM_MODEL", "gemini-3.6-flash"))
        audit.write_all(out["settlements"])

        unresolved = [s["txn_id"] for s in out["settlements"] if s["status"] != "matched"]
        ops.touch_first_seen(DATA_DIR, unresolved)
        st.session_state["drift"] = ops.check_drift(DATA_DIR, out["settlements"])
        rules_mod.record_run(DATA_DIR, out["metrics"], out["settlements"])
        ops.prepare_run(DATA_DIR, run_id, CURRENT_USER)

        lines = ticker_lines(out["metrics"], out["settlements"])
        _ticker_ms = min(motion.TICKER_CAP_MS, len(lines) * motion.TICKER_STAGGER_MS + 400)
        st.session_state["processing_reveal"] = {
            "lines": lines,
            "duration_s": max(_ticker_ms, motion.PIPELINE_FLOW_TOTAL_MS) / 1000,
        }
        return True, None
    except Exception as exc:  # noqa: BLE001 - the whole point is to catch everything here
        retry_placeholder.empty()
        import traceback
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "error.log"), "a") as f:
            f.write(f"\n--- {datetime.now(timezone.utc).isoformat()} ---\n")
            f.write(traceback.format_exc())
        return False, exc.__class__.__name__


TOUR_STEPS = [
    {"tab": "Overview", "title": "1 / 5 — Instrument panel",
     "text": "Auto-match rate, precision, recall, and the layer breakdown for this run — "
            "your instrument panel before you trust anything downstream."},
    {"tab": "Reconciliation", "title": "2 / 5 — The pipeline, live",
     "text": "Five layers ran in order, cheapest and most certain first. Expand any row for "
            "a plain-English \"because X\" rationale and the exact arithmetic behind it."},
    {"tab": "A/B: LLM impact", "title": "3 / 5 — Does the LLM earn its place?",
     "text": "The LLM only ever sees what layers 1-4 couldn't close. This tab scores its "
            "proposals against hidden ground truth — real wins and real false positives, "
            "not just a claimed accuracy number."},
    {"tab": "Model diagnostics", "title": "4 / 5 — Is the confidence trustworthy?",
     "text": "Calibration (does 90% confidence mean 90% correct?) and honeypots (adversarial "
            "credits designed to bait a false match) — both measured, neither hidden."},
    {"tab": "Reports", "title": "5 / 5 — Export and audit",
     "text": "Every decision is in a hash-chained audit log. Export CSV/JSONL, or build a "
            "one-click PDF scorecard from a fresh 5-seed evaluation."},
]


def render_tour_overlay() -> None:
    """In-flow guided-tour banner at the top of the dashboard — deliberately
    NOT position:fixed. An earlier version floated it bottom-left, which
    sat directly on top of the sidebar's own screen region and made its
    buttons unreliable to click; an ordinary block can't collide with
    anything. Advancing a step just changes active_tab and re-renders; it
    never fakes data, it only narrates the real dashboard a manual
    click-through would show."""
    step_i = st.session_state.get("tour_step", 0)
    step = TOUR_STEPS[step_i]
    with st.container(key="tour_overlay"):
        st.markdown(f'<div class="rp-tour-title">{icon("flag", 15)}{step["title"]}</div>'
                   f'<div class="rp-tour-text">{step["text"]}</div>', unsafe_allow_html=True)
        tc1, tc2, tc3, _spacer = st.columns([1, 1, 1, 5])
        with tc1:
            if step_i > 0 and st.button("Back", key="tour_back", width="stretch"):
                st.session_state["tour_step"] = step_i - 1
                st.session_state["active_tab"] = TOUR_STEPS[step_i - 1]["tab"]
                st.rerun()
        with tc2:
            if st.button("Skip tour", key="tour_skip", width="stretch", type="tertiary"):
                st.session_state["tour_active"] = False
                st.rerun()
        with tc3:
            if step_i < len(TOUR_STEPS) - 1:
                if st.button("Next", key="tour_next", type="primary", width="stretch"):
                    st.session_state["tour_step"] = step_i + 1
                    st.session_state["active_tab"] = TOUR_STEPS[step_i + 1]["tab"]
                    st.rerun()
            else:
                if st.button("Done", key="tour_done", type="primary", width="stretch"):
                    st.session_state["tour_active"] = False
                    st.rerun()


def pipeline_flow_nodes(settlements: list[dict]) -> list[dict]:
    """Real per-layer counts, split into the 5 conceptual pipeline stages
    (matches mini_pipeline's pre-run labels) for the animated flow diagram —
    every count here is read straight off this run's actual settlements,
    never fabricated. The 5th node is the terminal exception count, not a
    layer, since 'still unresolved after everything' is itself a real,
    honest outcome worth showing lit up last."""
    from collections import Counter
    counts = Counter(s["layer"] for s in settlements)
    return [
        {"label": "Exact match", "count": counts.get("exact_reference+deduction_engine", 0)},
        {"label": "Batch / subset-sum", "count": counts.get("anchored_batch_completion", 0)
                                                 + counts.get("amount_date_subset_sum", 0)
                                                 + counts.get("exact_reference_batch+deduction_engine", 0)},
        {"label": "Partial payments", "count": counts.get("exact_reference+partial_payment", 0)},
        {"label": "LLM investigator", "count": counts.get("llm_investigator", 0)},
        {"label": "Unresolved", "count": sum(1 for s in settlements if s["status"] == "exception")},
    ]


def start_confirm_animation(txn_ids: list[str], layer_by_txn: dict[str, str]) -> None:
    """Entry point shared by single Accept, grouped bulk approve, and the
    bulk-selection bar's Confirm — sets state and reruns; the actual state
    mutation happens once, at the end of the Reconciliation tab, after the
    animation has had time to play (#3, #4)."""
    st.session_state["confirm_anim"] = {"txn_ids": list(txn_ids), "layer_by_txn": layer_by_txn}
    st.rerun()


# ==========================================================================
# SIDEBAR
# ==========================================================================
with st.sidebar:
    st.markdown("**Data**")
    n = st.slider("Invoices to generate", 30, 150, 60, step=10)
    seed = st.number_input("Random seed", value=42, step=1)

    # item 7: scenario injector -- force specific scenario types to appear
    # in the next generated batch, useful for controlling exactly what shows
    # up during a live demo instead of hoping the random mix cooperates.
    from data_gen import FORCEABLE_SCENARIOS
    SCENARIO_LABEL = {
        "garbled_reference": "Garbled reference", "batched_settlement": "Batched settlement",
        "partial_payment": "Partial payment", "dropped_reference": "Dropped reference",
        "honeypot": "Honeypot / adversarial case",
    }
    forced_scenarios = st.multiselect(
        "Force-include scenarios in next batch", FORCEABLE_SCENARIOS,
        format_func=lambda s: SCENARIO_LABEL.get(s, s), key="forced_scenarios",
        help="Guarantees at least one of each selected type appears — the default random mix "
            "may or may not include a given type at small batch sizes.")

    if st.button("Regenerate synthetic data", width="stretch"):
        generate(int(n), int(seed), DATA_DIR, force_scenarios=set(forced_scenarios))
        for k in ("recon_out", "confirmed", "rejected", "selected_txn", "run_id",
                 "seen_txn_ids", "kpi_animated_runs", "processing_reveal", "auto_seeded"):
            st.session_state.pop(k, None)
        st.success(f"Generated {n} invoices and settlement transactions."
                  + (f" Forced: {', '.join(SCENARIO_LABEL.get(s, s) for s in forced_scenarios)}."
                     if forced_scenarios else ""))

    # ---- guided tour: seeds every interesting scenario, runs the real
    # pipeline (same run_pipeline_once() the manual button uses, never a
    # fake/staged run), then walks the actual dashboard tab by tab. Exists
    # so a judge opening the deployed link cold gets a self-explaining demo
    # instead of an empty batch and a blank stare.
    if st.button("🧭 Take the tour", width="stretch",
                help="Seeds a batch with every scenario type and walks you through the dashboard."):
        generate(80, 777, DATA_DIR, force_scenarios=set(FORCEABLE_SCENARIOS))
        for k in ("recon_out", "confirmed", "rejected", "selected_txn", "run_id",
                 "seen_txn_ids", "kpi_animated_runs", "processing_reveal", "auto_seeded"):
            st.session_state.pop(k, None)
        _tour_retry = st.empty()
        _tour_use_llm = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        ok, err = run_pipeline_once(_tour_use_llm, _tour_retry)
        _tour_retry.empty()
        if ok:
            st.session_state["tour_active"] = True
            st.session_state["tour_step"] = 0
            st.session_state["active_tab"] = TOUR_STEPS[0]["tab"]
            st.rerun()
        else:
            st.error(f"Couldn't start the tour: {err}. See data/error.log for details.")

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    api_key_present = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    use_llm = st.checkbox("Use live Gemini API for exception investigation", value=api_key_present)
    if use_llm and not api_key_present:
        st.caption("No GEMINI_API_KEY — falling back to the rule-based investigator.")
    elif use_llm:
        st.caption("GEMINI_API_KEY detected — live Gemini calls enabled.")

    if st.session_state.get("processing_reveal"):
        # ---- #5: pipeline ticker replaces the button while "processing" ---
        motion.pipeline_ticker(st.session_state["processing_reveal"]["lines"],
                               height=28 * (len(st.session_state["processing_reveal"]["lines"]) + 1))
    elif st.button("Run reconciliation", type="primary", width="stretch"):
        # ---- item 5: global error boundary around the main pipeline run ---
        # A judge live-demoing this must never see a raw Streamlit traceback.
        # Anything unhandled here is caught, logged in full to a local file,
        # and shown as one calm sentence instead. Shared with Take the tour
        # via run_pipeline_once() so the two paths can't drift apart.
        retry_placeholder = st.empty()
        ok, err = run_pipeline_once(use_llm, retry_placeholder)
        retry_placeholder.empty()
        if ok:
            st.rerun()
        else:
            st.error(f"Something went wrong while running the pipeline: {err}. "
                    f"Your data and previous results are unaffected. Full details logged to "
                    f"data/error.log for debugging.")

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**Autonomy per rule**")
    _rules = rules_mod.load_rules(DATA_DIR)
    for layer, rule in _rules.items():
        if not rule.tunable:
            continue
        new_autonomy = st.selectbox(
            rule.label, rules_mod.AUTONOMY_LEVELS,
            index=rules_mod.AUTONOMY_LEVELS.index(rule.autonomy),
            format_func=lambda a: rules_mod.AUTONOMY_LABEL[a], key=f"autonomy_{layer}")
        new_threshold = rule.threshold
        if new_autonomy == "auto_under_threshold":
            new_threshold = st.slider("Confidence floor to auto-confirm", 50, 99, rule.threshold, key=f"thr_{layer}")
        if new_autonomy != rule.autonomy or new_threshold != rule.threshold:
            _rules[layer] = rules_mod.Rule(layer=layer, label=rule.label, autonomy=new_autonomy,
                                           threshold=new_threshold, tunable=True)
            rules_mod.save_rules(DATA_DIR, _rules)
            st.rerun()

# --------------------------------------------------------------------------
# Empty states
# --------------------------------------------------------------------------
if not os.path.exists(INV_CSV):
    # Cold start (a fresh clone, or a fresh Streamlit Cloud deploy, has no
    # data/ directory at all — it's gitignored on purpose, see .gitignore).
    # A first-time visitor should land on a working demo, not an empty info
    # banner with no context — so seed the default batch automatically,
    # once, using the sidebar's own current n/seed, and say so plainly.
    generate(int(n), int(seed), DATA_DIR)
    st.session_state["auto_seeded"] = True
if st.session_state.get("auto_seeded") and "recon_out" not in st.session_state:
    st.info(f"Welcome — this is an AI reconciliation agent demo. We generated a starter batch "
           f"({n} invoices × settlements, seed {seed}) for you automatically. Click "
           f"**Run reconciliation** in the sidebar to see it work, or **Regenerate synthetic data** "
           f"for a different mix.")
if "recon_out" not in st.session_state:
    # ---- STATE 1: intent preview, replacing the old blank info banner -----
    _rules_preview = rules_mod.load_rules(DATA_DIR)
    _llm_rule = _rules_preview.get(rules_mod.TUNABLE_LAYER)
    if _llm_rule and _llm_rule.autonomy == "suggest_only":
        _step5 = "LLM investigator (suggest-only)"
    elif _llm_rule and _llm_rule.autonomy == "auto_under_threshold":
        _step5 = f"LLM investigator (auto-confirm ≥ {_llm_rule.threshold})"
    else:
        _step5 = "LLM investigator (auto-confirm)"

    def _row_count(path: str) -> int:
        with open(path) as f:
            return max(0, sum(1 for _ in f) - 1)

    _n_inv = _row_count(INV_CSV)
    _n_settle = _row_count(SETTLE_CSV)

    st.markdown(
        f'<div class="rp-card">'
        f'<div style="display:flex;align-items:center;gap:8px;font-weight:600;color:#012652">'
        f'{icon("check-circle", 16)}What happens when you click Run</div>'
        f'{mini_pipeline(_step5)}'
        f'<div style="color:#6B7280;font-size:12.5px;margin-top:6px">'
        f'{_n_inv} invoices × {_n_settle} settlements, seed {seed} — nothing is written until you '
        f'confirm results below.</div></div>',
        unsafe_allow_html=True,
    )
    chat_widget.render("pre_run", use_llm=use_llm)
    st.stop()

# ---- #5/#6: while the ticker plays in the sidebar, show skeleton here too -
if st.session_state.get("processing_reveal"):
    with st.container(key="topnav"):
        st.markdown("##### Reconciliation")
        st.caption("Processing…")
    # ---- pipeline flow: the actual thesis (5 stages, most of the volume
    # closes before the LLM ever sees it) shown as the centerpiece, not a
    # sidebar ticker — real per-layer counts from the run that just
    # finished, see pipeline_flow_nodes()'s docstring.
    _flow_nodes = pipeline_flow_nodes(st.session_state["recon_out"]["settlements"])
    motion.pipeline_flow(_flow_nodes)
    time.sleep(st.session_state["processing_reveal"]["duration_s"])
    del st.session_state["processing_reveal"]
    st.rerun()
    st.stop()

out = st.session_state["recon_out"]
metrics = out["metrics"]
settlements = out["settlements"]
invoices = out["invoices"]
enriched = st.session_state.get("enriched", {})
rules = rules_mod.load_rules(DATA_DIR)
auto_confirmed_by_rule = rules_mod.apply_autonomy(settlements, rules)
run_id = st.session_state["run_id"]
locked = ops.is_locked(DATA_DIR, run_id)
confirm_anim = st.session_state.get("confirm_anim")
confirm_anim_ids = confirm_anim["txn_ids"] if confirm_anim else []


def effective_status(s: dict) -> str:
    if s["txn_id"] in st.session_state["rejected"]:
        return "exception"
    if s["status"] == "pending_confirmation" and (
            s["txn_id"] in st.session_state["confirmed"] or s["txn_id"] in auto_confirmed_by_rule):
        return "matched"
    return s["status"]


def pct(key: str) -> str:
    v = metrics.get(key)
    return f"{v*100:.1f}%" if v is not None else "n/a"


def badge_wrap(txn_id: str, html: str) -> str:
    """#8: soft fade-in the first time a decision badge is ever rendered in
    this run; every subsequent rerun (filter change, etc.) shows it flat,
    with no replay."""
    fresh = txn_id not in st.session_state["seen_txn_ids"]
    cls = "rp-badge-fresh" if fresh else ""
    return f'<span class="{cls}">{html}</span>'


# ==========================================================================
# TOP NAV
# ==========================================================================
with st.container(key="topnav"):
    nc1, nc2 = st.columns([6, 1])
    with nc1:
        st.markdown("##### Reconciliation")
        st.markdown(
            f'<span style="color:#6B7280;font-size:12.5px">Run {run_id} · '
            f'{metrics["total_settlements"]} records · '
            f'<a href="https://sayyamjain1301.github.io/RAZORPAY/" target="_blank" '
            f'style="color:#0D94FB;text-decoration:none">{icon("external-link", 11)}About this project</a></span>',
            unsafe_allow_html=True)
    with nc2:
        toggle_icon = ":material/light_mode:" if st.session_state["dark_mode"] else ":material/dark_mode:"
        if st.button("", icon=toggle_icon, key="theme_toggle", help="Toggle dark mode"):
            st.session_state["dark_mode"] = not st.session_state["dark_mode"]
            st.rerun()

dup = st.session_state.get("dup_check")
if dup and dup["is_duplicate"]:
    st.warning(f"Duplicate ingestion detected — this settlements file (hash `{dup['hash']}`) "
              f"has been processed {dup['times_seen_before']} time(s) before.")

drift = st.session_state.get("drift") or []
if drift:
    with st.expander(f"{len(drift)} reconciliation(s) drifted from their last run"):
        st.caption("Same txn_id, different outcome than last time — investigate before trusting either run.")
        for d in drift[:15]:
            st.markdown(f"`{d['txn_id']}`: was **{d['was']['status']}** → **{d['now']['status']}**")

# ==========================================================================
# #10 — custom tab bar with a sliding Dodger Blue underline
# ==========================================================================
TAB_NAMES = ["Overview", "Reconciliation", "A/B: LLM impact", "Settlements",
            "Model diagnostics", "Reports", "Settings"]
active = st.session_state["active_tab"]
active_idx = TAB_NAMES.index(active) if active in TAB_NAMES else 0
n_tabs = len(TAB_NAMES)

st.markdown(
    f'<div class="rp-tabbar" style="position:relative">'
    f'<div class="rp-tabbar-underline" style="left:{active_idx/n_tabs*100:.4f}%;'
    f'width:{100/n_tabs:.4f}%"></div></div>',
    unsafe_allow_html=True,
)
tab_cols = st.columns(n_tabs)
for i, name in enumerate(TAB_NAMES):
    with tab_cols[i]:
        st.markdown(f'<div class="rp-tabbtn{" rp-tabbtn-active" if i == active_idx else ""}">',
                   unsafe_allow_html=True)
        if st.button(name, key=f"tabbtn_{name}", width="stretch", type="tertiary"):
            st.session_state["active_tab"] = name
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

active_tab = st.session_state["active_tab"]

# ---- persistent floating assistant — every tab of the dashboard state ----
_focused_txn = None
_expanded = st.session_state.get("expanded_rows", set())
if len(_expanded) == 1:
    _focused_txn = next(iter(_expanded))
chat_widget.render("dashboard", metrics=metrics, settlements=settlements,
                   focused_txn=_focused_txn, active_tab=active_tab, use_llm=use_llm)

if st.session_state.get("tour_active"):
    render_tour_overlay()

# ==========================================================================
# OVERVIEW
# ==========================================================================
if active_tab == "Overview":
    history = rules_mod.load_run_history(DATA_DIR)
    prev_rate = history[-2]["auto_match_rate"] if len(history) >= 2 else None
    delta = f"{(metrics['auto_match_rate'] - prev_rate)*100:+.1f}pp vs last run" if prev_rate is not None else None

    def anim_once(token: str, index: int = 0) -> str:
        """Entry-animation class + inline stagger delay, but only the first
        time this run renders `token`. Changing the date filter or coming
        back to this tab shows everything flat, exactly like badge_wrap()
        and the hero count-up already behave — motion marks a genuinely new
        state, never a re-render."""
        key = f"{run_id}:anim:{token}"
        if key in st.session_state["kpi_animated_runs"]:
            return ""
        st.session_state["kpi_animated_runs"].add(key)
        return f'class="rp-rise" style="animation-delay:{index * motion.RISE_STAGGER_MS}ms"'

    hc, sc = st.columns([1, 2])
    with hc:
        st.caption("Auto-match rate")
        # ---- #1: hero KPI count-up, once per run_id, never on re-render ---
        if run_id not in st.session_state["kpi_animated_runs"]:
            # Tween from the PREVIOUS run's rate when there is one, so the
            # number travels the distance this run actually moved it --
            # a 0 -> current count-up would hide that entirely.
            motion.count_up(metrics["auto_match_rate"] * 100,
                            start=(prev_rate * 100) if prev_rate is not None else 0.0,
                            decimals=1, suffix="%",
                            font_size="2.3rem", color="#0D94FB", height=55, elem_id=f"kpi_{run_id}")
            st.session_state["kpi_animated_runs"].add(run_id)
        else:
            st.markdown(f"<div style='font:700 2.3rem/1.1 Inter,sans-serif;color:#0D94FB;"
                       f"font-variant-numeric:tabular-nums'>{metrics['auto_match_rate']*100:.1f}%</div>",
                       unsafe_allow_html=True)
        if delta:
            st.caption(delta)
    with sc:
        s1, s2, s3 = st.columns(3)
        s1.markdown(f"<div class='rp-empty'>Exceptions</div><div style='font-size:1.3rem;color:#1A1F2B'>"
                   f"{metrics['exception']}</div>", unsafe_allow_html=True)
        s2.markdown(f"<div class='rp-empty'>Pending review</div><div style='font-size:1.3rem;color:#1A1F2B'>"
                   f"{metrics['pending_confirmation']}</div>", unsafe_allow_html=True)
        s3.markdown(f"<div class='rp-empty'>Records</div><div style='font-size:1.3rem;color:#1A1F2B'>"
                   f"{metrics['total_settlements']}</div>", unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    c5, c6, c7 = st.columns(3)
    c5.metric("Precision", pct("precision"))
    c6.metric("Recall", pct("recall"))
    c7.metric("Deduction-hypothesis accuracy", pct("deduction_hypothesis_accuracy"))

    # ---- value-weighted exposure: the money view, not the row view -------
    # Reconciliation practice reads the unreconciled *balance* first and the
    # row count second -- ten small exceptions carry less risk than one
    # large unresolved variance. Everything above this line is
    # record-weighted; everything below is rupee-weighted.
    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)

    # the date filter genuinely scopes the value analytics below (it used to
    # be rendered but wired to nothing at all)
    _dates = [enriched.get(s["txn_id"], {}).get("txn_date") for s in settlements]
    _dates = sorted({d for d in _dates if d})
    scoped = settlements
    if len(_dates) >= 2:
        vh1, vh2 = st.columns([2, 1])
        with vh1:
            st.markdown("**Value & exposure**")
            st.caption("Rupee-weighted view of the same batch. The pipeline-quality metrics "
                      "above cover the whole run; this section respects the date range.")
        with vh2:
            _lo, _hi = pd.to_datetime(_dates[0]).date(), pd.to_datetime(_dates[-1]).date()
            _picked = st.date_input("Settlement date range", value=(_lo, _hi),
                                    min_value=_lo, max_value=_hi, key="ov_date_range")
        if isinstance(_picked, (tuple, list)) and len(_picked) == 2:
            _from, _to = _picked
            scoped = [s for s in settlements
                     if (_d := enriched.get(s["txn_id"], {}).get("txn_date"))
                     and _from <= pd.to_datetime(_d).date() <= _to]
    else:
        st.markdown("**Value & exposure**")

    if not scoped:
        st.markdown('<p class="rp-empty">No settlements in the selected date range.</p>',
                   unsafe_allow_html=True)
    else:
        vs = insights.value_summary(scoped, enriched, effective_status)
        VALUE_TILES = [
            ("Total settled", vs["total_value"], "#012652", None),
            ("Reconciled", vs["matched_value"], "#16A34A", vs["matched_value_pct"]),
            ("Awaiting review", vs["pending_value"], "#D97706", None),
            ("Unreconciled", vs["exception_value"], "#DC2626", None),
        ]
        vcols = st.columns(4)
        for _i, (col, (label, amount, color, share)) in enumerate(zip(vcols, VALUE_TILES)):
            sub = f"{share*100:.1f}% of value" if share is not None else \
                  (f"{amount / vs['total_value'] * 100:.1f}% of value" if vs["total_value"] else "—")
            with col:
                st.markdown(
                    f'<div {anim_once(f"vtile{_i}", _i)}>'
                    f'<div class="rp-card" style="padding:12px 14px">'
                    f'<div style="color:#6B7280;font-size:11.5px">{label}</div>'
                    f'<div class="rp-amount" style="font-size:1.35rem;color:{color};margin-top:3px">'
                    f'{fmt_inr_compact(amount)}</div>'
                    f'<div style="color:#6B7280;font-size:11px;margin-top:2px">{sub}</div>'
                    f'</div></div>', unsafe_allow_html=True)

        # the headline insight: when rupees and rows disagree, say so plainly
        _gap = vs["value_vs_count_pp"]
        if abs(_gap) >= 5:
            if _gap < 0:
                st.warning(f"**Value is reconciling worse than volume.** "
                          f"{vs['matched_count_pct']*100:.1f}% of records auto-reconciled but only "
                          f"{vs['matched_value_pct']*100:.1f}% of value ({abs(_gap):.1f}pp behind) — "
                          f"the larger-ticket settlements are disproportionately the stuck ones, so "
                          f"{fmt_inr(vs['at_risk_value'])} is still exposed. Clear by value, not by row count.")
            else:
                st.success(f"**Value is reconciling better than volume.** "
                          f"{vs['matched_value_pct']*100:.1f}% of value cleared vs "
                          f"{vs['matched_count_pct']*100:.1f}% of records ({_gap:.1f}pp ahead) — "
                          f"what's left unresolved is mostly small-ticket.")
        else:
            st.caption(f"Value and volume are tracking together (within {abs(_gap):.1f}pp) — "
                      f"no size bias in what's failing to reconcile. "
                      f"{fmt_inr(vs['at_risk_value'])} still exposed across "
                      f"{vs['pending_count'] + vs['exception_count']} record(s).")

        # ---- fee/tax leakage  |  aging of unresolved ---------------------
        st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
        lc, ac = st.columns(2)

        with lc:
            st.markdown("**Where the money went**")
            leak = insights.deduction_leakage(scoped, invoices, enriched, effective_status)
            if leak["n_settlements"]:
                st.caption(f"Across {leak['n_settlements']} reconciled settlement(s) with a "
                          f"detected deduction formula.")
                rows = [("Gross invoiced", leak["gross"], "#1A1F2B", ""),
                       ("Gateway fees", -leak["gateway_fees"], "#D97706", "deducted"),
                       ("GST on fees", -leak["gst_on_fees"], "#D97706", "deducted"),
                       ("TDS withheld", -leak["tds"], "#D97706", "deducted"),
                       ("Net received", leak["net_received"], "#16A34A", "")]
                _fee_anim = bool(anim_once("feerows"))
                body = "".join(
                    f'<div style="display:flex;justify-content:space-between;padding:5px 0;'
                    f'border-bottom:1px solid #E5E8EC;font-size:12.5px'
                    f'{f";animation:rp-rise-in {motion.RISE_MS}ms ease-out {_ri * motion.RISE_STAGGER_MS}ms both" if _fee_anim else ""}">'
                    f'<span style="color:#6B7280">{label}'
                    f'{f" <span style=\'font-size:10.5px\'>({note})</span>" if note else ""}</span>'
                    f'<span class="rp-amount" style="color:{color}">{fmt_inr(abs(amt))}</span></div>'
                    for _ri, (label, amt, color, note) in enumerate(rows))
                st.markdown(f'<div class="rp-card">{body}'
                           f'<div style="margin-top:8px;font-size:11.5px;color:#6B7280">'
                           f'Effective deduction rate: <strong style="color:#012652">'
                           f'{leak["effective_rate"]*100:.2f}%</strong> of gross</div></div>',
                           unsafe_allow_html=True)
            else:
                st.markdown('<p class="rp-empty">No reconciled settlement in range carried a '
                          'deduction formula — nothing to break down.</p>', unsafe_allow_html=True)

        with ac:
            st.markdown("**Aging of unreconciled items**")
            unresolved = [s for s in scoped if effective_status(s) != "matched"]
            if unresolved:
                ag = insights.aging(unresolved, enriched)
                st.caption(f"Measured from the date each settlement actually landed. "
                          f"Oldest open item: **{ag['oldest_days']} days**.")
                _max_v = max([b["value"] for b in ag["buckets"].values()] or [0]) or 1
                BUCKET_COLOR = {"0-7 days": "#16A34A", "8-14 days": "#0D94FB",
                               "15-30 days": "#D97706", "30+ days": "#DC2626"}
                bars = ""
                for label, b in ag["buckets"].items():
                    pctw = b["value"] / _max_v * 100
                    bars += (
                        f'<div style="margin-bottom:9px">'
                        f'<div style="display:flex;justify-content:space-between;font-size:11.5px;'
                        f'margin-bottom:3px"><span style="color:#1A1F2B">{label} '
                        f'<span style="color:#6B7280">({b["count"]})</span></span>'
                        f'<span class="rp-amount" style="color:#012652">{fmt_inr_compact(b["value"])}</span></div>'
                        f'<div style="height:6px;background:#E5E8EC;border-radius:3px;overflow:hidden">'
                        f'<div class="rp-layerbar-seg" style="width:{pctw:.2f}%;height:100%;'
                        f'background:{BUCKET_COLOR[label]}"></div></div></div>')
                if ag["undated"]["count"]:
                    bars += (f'<div style="font-size:11px;color:#6B7280;margin-top:4px">'
                            f'{ag["undated"]["count"]} item(s) with no usable date</div>')
                st.markdown(f'<div class="rp-card">{bars}</div>', unsafe_allow_html=True)
            else:
                st.markdown('<p class="rp-empty">Nothing unreconciled in this range — '
                          'the whole batch closed.</p>', unsafe_allow_html=True)

        # ---- prioritized worklist: biggest exposure first ----------------
        unresolved = [s for s in scoped if effective_status(s) != "matched"]
        if unresolved:
            st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
            st.markdown("**Clear these first — largest exposure**")
            conc = insights.concentration(unresolved, enriched, top_n=5)
            top = insights.top_exposure(unresolved, enriched, limit=5)
            st.caption(f"The top {len(top)} of {len(unresolved)} unresolved item(s) carry "
                      f"**{conc*100:.0f}%** of the total {fmt_inr(vs['at_risk_value'])} exposure "
                      f"— clearing them removes most of the risk.")
            hdr = ('<div class="rp-drawer-row head"><span>Transaction</span><span>Counterparty</span>'
                  '<span>Status</span><span>Amount</span></div>')
            _wl_anim = bool(anim_once("worklist"))
            body = "".join(
                f'<div class="rp-drawer-row"'
                f'{f" style=\'animation:rp-rise-in {motion.RISE_MS}ms ease-out {_wi * motion.RISE_STAGGER_MS}ms both\'" if _wl_anim else ""}>'
                f'<span class="rp-mono">{r["txn_id"]}</span>'
                f'<span>{r["counterparty"] or "—"}</span>'
                f'<span>{STATUS_LABEL.get(r["status"], r["status"])}</span>'
                f'<span class="rp-amount">{fmt_inr(r["amount"])}</span></div>'
                for _wi, r in enumerate(top))
            st.markdown(f'<div class="rp-card">{hdr}{body}</div>', unsafe_allow_html=True)

    # ---- how it resolved / how it's trending, side by side ---------------
    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    # Streamlit's native charts render with no entry of their own. Gate the
    # rise-in behind the same once-per-run token as everything else, by
    # injecting the rule only on the render that should animate — a static
    # rule would replay every time a filter moved.
    if anim_once("charts"):
        st.markdown(f"<style>.st-key-ov_charts {{ animation: rp-rise-in {motion.RISE_MS}ms "
                   f"ease-out 80ms both; }}</style>", unsafe_allow_html=True)
    _charts = st.container(key="ov_charts")
    bc1, bc2 = _charts.columns(2)
    with bc1:
        st.markdown("**Resolution layer breakdown**")
        layer_counts = pd.Series([LAYER_LABEL.get(s["layer"], s["layer"])
                                 for s in settlements]).value_counts()
        st.bar_chart(layer_counts, color="#0D94FB", height=260)
    with bc2:
        if len(history) >= 2:
            st.markdown("**Auto-match rate across runs**")
            _hist = pd.DataFrame(history)
            _hist.index = range(1, len(_hist) + 1)
            _hist.index.name = "Run"
            st.line_chart(_hist[["auto_match_rate"]], color="#0D94FB", height=260)
        else:
            st.markdown("**Auto-match rate across runs**")
            st.markdown('<p class="rp-empty">Only one run so far — run reconciliation again '
                       '(or change the seed) to start a trend line.</p>', unsafe_allow_html=True)

# ==========================================================================
# RECONCILIATION
# ==========================================================================
elif active_tab == "Reconciliation":
    # ======================================================================
    # STATE 2 — results pane. KPI row -> clickable layer bar -> 4 tabs ->
    # a pinned, collapsible activity-log drawer.
    # ======================================================================
    history = rules_mod.load_run_history(DATA_DIR)
    prev_run = history[-2] if len(history) >= 2 else None

    def _delta_pp(key: str):
        if prev_run is None or metrics.get(key) is None or prev_run.get(key) is None:
            return None
        return (metrics[key] - prev_run[key]) * 100

    KPI_DEFS = [
        ("Auto-match rate", "auto_match_rate"), ("Resolved rate", "resolved_rate"),
        ("Precision", "precision"), ("Recall", "recall"),
        ("Deduction accuracy", "deduction_hypothesis_accuracy"),
    ]
    kcols = st.columns(5)
    for col, (label, key) in zip(kcols, KPI_DEFS):
        val = metrics.get(key)
        val_str = f"{val*100:.1f}%" if val is not None else "n/a"
        # soft pop-in only the first time THIS run shows THIS metric — a
        # filter change or tab switch re-render shows it flat, same idiom
        # as badge_wrap()'s decision-badge fade-in above.
        anim_key = f"{run_id}:{key}"
        fresh_cls = "" if anim_key in st.session_state["kpi_animated_runs"] else "rp-kpi-fresh"
        st.session_state["kpi_animated_runs"].add(anim_key)
        spark_vals = [h[key] for h in history[-8:] if h.get(key) is not None]
        spark = sparkline_svg(spark_vals) if len(spark_vals) >= 2 else ""
        with col:
            st.markdown(
                f'<div class="rp-card" style="padding:12px 14px">'
                f'<div style="display:flex;align-items:center;gap:6px;color:#6B7280;font-size:11.5px">'
                f'{icon("check-circle", 13)}{label}</div>'
                f'<div class="rp-amount {fresh_cls}" style="font-size:1.4rem;color:#012652;margin-top:3px">{val_str}</div>'
                f'<div style="display:flex;align-items:center;justify-content:space-between;margin-top:3px">'
                f'<span>{trend_arrow(_delta_pp(key))}</span>{spark}</div>'
                f'</div>', unsafe_allow_html=True)

    _run_ts = run_id.replace("run_", "")
    try:
        from datetime import datetime as _dt
        _run_ts = _dt.strptime(_run_ts, "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        pass
    st.markdown(
        f'<p style="color:#6B7280;font-size:12px;margin:10px 0 16px">'
        f'Run on seed {seed} · n={metrics["total_settlements"]} · {_run_ts} · not live data</p>',
        unsafe_allow_html=True)

    # ---- slim cost/timing strip ----------------------------------------
    # Gemini 3.6 Flash published rates (USD/M tokens), introductory pricing
    # in effect through 2026-12-31 ($1.50/$7.50 standard rate applies from
    # 2027-01-01 -- update this constant then). Output tokens already
    # include thinking tokens (llm_reasoner.py folds them in at the source),
    # since Google bills thinking at the output-token rate too. FX: 88
    # INR/USD, same constant used throughout this project.
    _GEMINI_IN, _GEMINI_OUT, _FX = 0.75, 3.75, 88.0
    _det_n = sum(1 for s in settlements if s["source"] != "llm")
    _llm_n = sum(1 for s in settlements if s["source"] == "llm")
    _in_tok = sum(s.get("input_tokens", 0) for s in settlements)
    _out_tok = sum(s.get("output_tokens", 0) for s in settlements)
    _usd = _in_tok / 1e6 * _GEMINI_IN + _out_tok / 1e6 * _GEMINI_OUT
    _wall_ms = st.session_state.get("wall_clock_ms")
    _wall_str = f"{_wall_ms:g}ms" if _wall_ms is not None else "n/a"
    st.markdown(
        f'<div class="rp-card rp-mono" style="padding:8px 14px;font-size:11.5px;'
        f'color:#1A1F2B;background:#F5F7FA;margin-bottom:16px">'
        f'Deterministic: {_det_n} records (₹0 cost) · '
        f'LLM: {_llm_n} records ({_in_tok + _out_tok} tokens, ₹{_usd*_FX:.2f}) · '
        f'Wall-clock: {_wall_str}</div>',
        unsafe_allow_html=True)

    # ---- clickable layer-breakdown bar --------------------------------
    from collections import Counter as _Counter
    layer_counts = _Counter(s["layer"] for s in settlements)
    total_n = sum(layer_counts.values()) or 1
    # Every shade here is Dodger Blue or Prussian Blue at a different
    # opacity, never a new hue -- "the only accent" per ui_theme.py's brand
    # rule still holds; only alpha varies, so 6 layers stay distinguishable
    # without introducing off-palette blues.
    LAYER_COLOR = {
        "exact_reference+deduction_engine": "rgba(13,148,251,1)",
        "exact_reference+partial_payment": "rgba(13,148,251,0.7)",
        "exact_reference_batch+deduction_engine": "rgba(13,148,251,0.4)",
        "anchored_batch_completion": "rgba(1,38,82,1)",
        "amount_date_subset_sum": "rgba(1,38,82,0.6)",
        "llm_investigator": "#6B7280",  # SLATE -- the one non-deterministic layer, neutral by design
    }
    st.session_state.setdefault("layer_filter_active", None)
    segs_html = "".join(
        f'<div class="rp-layerbar-seg" style="width:{c/total_n*100:.3f}%;'
        f'background:{LAYER_COLOR.get(l, "#6B7280")}"></div>'
        for l, c in layer_counts.items()
    )
    st.markdown(f'<div class="rp-layerbar">{segs_html}</div>', unsafe_allow_html=True)
    leg_cols = st.columns(len(layer_counts))
    for col, (l, c) in zip(leg_cols, layer_counts.items()):
        with col:
            is_active = st.session_state["layer_filter_active"] == l
            if st.button(f"{LAYER_LABEL.get(l, l)} ({c})", key=f"layerseg_{l}",
                        type="primary" if is_active else "tertiary", width="stretch"):
                st.session_state["layer_filter_active"] = None if is_active else l
                st.rerun()
    if st.session_state["layer_filter_active"]:
        st.caption(f"Filtered to layer: `{st.session_state['layer_filter_active']}` "
                  f"— click the segment again to clear.")

    def _layer_ok(s: dict) -> bool:
        active = st.session_state["layer_filter_active"]
        return active is None or s["layer"] == active

    # ---- precision vs. auto-approval tradeoff card ----------------------
    if os.path.exists(GT_CSV):
        from recon_agent.matcher import load_ground_truth
        _gt = load_ground_truth(GT_CSV)
        _rule = rules.get("llm_investigator")
        _default_tau = _rule.threshold if _rule and _rule.autonomy == "auto_under_threshold" else 85
        with st.container(border=True):
            st.markdown("**Precision vs. auto-approval tradeoff**")
            _curve = rules_mod.threshold_sweep(settlements, _gt)
            if any(c["n"] > 0 for c in _curve):
                import altair as alt
                _cdf = pd.DataFrame(_curve)
                _tau_pick = st.slider("Threshold", 0, 100, _default_tau, step=5, key="tradeoff_tau",
                                      help="Draggable threshold marker — default matches the "
                                           "configured precision floor.")
                _base = alt.Chart(_cdf).encode(x=alt.X("tau:Q", title="Confidence threshold"))
                _line_p = _base.mark_line(color="#16A34A").encode(
                    y=alt.Y("precision:Q", title="Rate", axis=alt.Axis(format="%")))
                _line_c = _base.mark_line(color="#0D94FB").encode(y="coverage:Q")
                _rule_mark = alt.Chart(pd.DataFrame({"tau": [_tau_pick]})).mark_rule(
                    color="#012652", strokeDash=[3, 2]).encode(x="tau:Q")
                st.altair_chart((_line_p + _line_c + _rule_mark).properties(height=180),
                                width="stretch")
                _row = min(_curve, key=lambda r: abs(r["tau"] - _tau_pick))
                st.caption(f"At this threshold: {_row['coverage']*100:.1f}% auto-approved, "
                          f"{_row['precision']*100:.1f}% precision (n={_row['n']}). "
                          f"Blue = auto-approval rate, green = precision.")
            else:
                st.markdown('<p class="rp-empty">Not enough LLM-layer volume in this batch to '
                          'chart a tradeoff curve.</p>', unsafe_allow_html=True)

    # ---- search / filter bar --------------------------------------------
    fc1, fc2, fc3 = st.columns([2, 1, 1])
    with fc1:
        search_q = st.text_input("Search", key="recon_search", icon=":material/search:",
                                 label_visibility="collapsed", placeholder="Search txn or invoice ID")
    with fc2:
        band_filter = st.selectbox("Confidence band", ["All", "High", "Medium", "Low"],
                                   key="recon_band_filter", label_visibility="collapsed")
    with fc3:
        layer_options = ["All"] + sorted({s["layer"] for s in settlements})
        layer_filter_dd = st.selectbox("Layer", layer_options, key="recon_layer_dd",
                                       format_func=lambda l: LAYER_LABEL.get(l, l) if l != "All" else "All layers",
                                       label_visibility="collapsed")
    if layer_filter_dd != "All":
        st.session_state["layer_filter_active"] = layer_filter_dd

    def _search_ok(s: dict) -> bool:
        q = search_q.strip().upper()
        return not q or q in s["txn_id"].upper() or any(q in i.upper() for i in s["matched_invoice_ids"])

    def _band_ok(s: dict) -> bool:
        if band_filter == "All":
            return True
        c = s["confidence"]
        tier = "High" if c >= 80 else ("Medium" if c >= 55 else "Low")
        return tier == band_filter

    def autonomy_label_for(s: dict) -> str:
        if s["layer"] != "llm_investigator":
            return "Auto-closed"
        rule = rules.get("llm_investigator")
        if s["txn_id"] in auto_confirmed_by_rule:
            return f"Auto-confirmed by rule ({rule.label})" if rule else "Auto-confirmed by rule"
        if rule is None or rule.autonomy == "suggest_only":
            return "Suggest-only"
        if rule.autonomy == "auto_under_threshold":
            return f"Auto-confirm ≥ {rule.threshold}"
        return "Auto-confirm"

    def render_expand(s: dict, *, allow_escalate: bool = False):
        """Inline expand: settlement/invoice diff, owner, comments, audit
        trail + replay, Undo where applicable — replaces the old separate
        review panel with an in-place, per-row expansion."""
        info = enriched.get(s["txn_id"], {})
        matched_inv = [i for i in invoices if i["invoice_id"] in s["matched_invoice_ids"]]

        st.markdown(because_sentence(s, invoices), unsafe_allow_html=True)
        if info.get("narration"):
            st.code(info["narration"], language=None)

        if s["status"] == "matched" and matched_inv:
            st.markdown("**Show the math**")
            import re as _re
            label = s.get("deduction_label")
            if s["layer"] == "exact_reference+partial_payment" or not label or label == "none":
                # No deduction formula applies here — a plain match (or a
                # partial payment, where the invoice's own `amount` is the
                # full original total, not what this one settlement covers,
                # so a gross->net breakdown would be arithmetically wrong).
                note = ("Partial payment — no deduction formula applies; the balance "
                       "was reduced by the settlement amount directly."
                       if s["layer"] == "exact_reference+partial_payment"
                       else "No deduction detected — the settlement amount matched the "
                            "open balance exactly.")
                st.code(f"  settlement amount   {info.get('amount', 0):>12,.2f}\n  {note}", language=None)
            else:
                gross = sum(mi["amount"] for mi in matched_inv)
                math_lines = [f"  gross amount        {gross:>12,.2f}"]
                net = gross
                m_fee = _re.match(r"gateway_fee\(([\d.]+)%\)\+gst\(([\d.]+)%_on_fee\)", label)
                m_tds = _re.match(r"tds\((\d+)%\)", label)
                if m_fee:
                    fee_rate, gst_rate = float(m_fee.group(1)) / 100, float(m_fee.group(2)) / 100
                    fee = round(gross * fee_rate, 2)
                    gst = round(fee * gst_rate, 2)
                    math_lines.append(f"- fee @ {fee_rate*100:.1f}%       {fee:>12,.2f}")
                    math_lines.append(f"- GST @ {gst_rate*100:.0f}% on fee   {gst:>12,.2f}")
                    net = round(gross - fee - gst, 2)
                elif m_tds:
                    rate = float(m_tds.group(1)) / 100
                    tds = round(gross * rate, 2)
                    math_lines.append(f"- TDS @ {rate*100:.0f}%        {tds:>12,.2f}")
                    net = round(gross - tds, 2)
                math_lines.append("=" * 30)
                math_lines.append(f"  net (expected)      {net:>12,.2f}")
                math_lines.append(f"  settlement amount   {info.get('amount', net):>12,.2f}")
                st.code("\n".join(math_lines), language=None)

        if matched_inv:
            st.markdown("**Settlement vs. invoice**")
            diff_rows = []
            for mi in matched_inv:
                amt_settle = info.get("amount")
                amt_match = amt_settle is not None and abs(amt_settle - mi["remaining_amount"]) < 0.01
                diff_rows.append({
                    "Field": f"amount ({mi['invoice_id']})",
                    "Settlement": fmt_inr(amt_settle), "Invoice": fmt_inr(mi["amount"]),
                    "Match": "✓" if amt_match else "diff",
                })
                diff_rows.append({
                    "Field": "date", "Settlement": info.get("txn_date") or "—",
                    "Invoice": mi["invoice_date"], "Match": "—",
                })
                diff_rows.append({
                    "Field": "reference", "Settlement": info.get("narration", "")[:40],
                    "Invoice": mi["reference_code"],
                    "Match": "✓" if mi["reference_code"].upper() in (info.get("narration") or "").upper() else "—",
                })
            st.dataframe(pd.DataFrame(diff_rows), width="stretch", hide_index=True)

        oc1, oc2 = st.columns(2)
        with oc1:
            new_owner = st.selectbox("Owner", ops.OWNERS,
                                     index=ops.OWNERS.index(ops.get_owner(DATA_DIR, s["txn_id"]))
                                     if ops.get_owner(DATA_DIR, s["txn_id"]) in ops.OWNERS else 0,
                                     key=f"owner_{s['txn_id']}")
            if new_owner != ops.get_owner(DATA_DIR, s["txn_id"]):
                ops.set_owner(DATA_DIR, s["txn_id"], new_owner)
        with oc2:
            if s["txn_id"] in st.session_state.get("manually_escalated", set()):
                st.markdown(f"<span class='rp-status-row'><span class='rp-dot rp-dot-red'></span>"
                           f"Escalated to review queue</span>", unsafe_allow_html=True)

        with st.expander(f"Comments ({len(ops.get_comments(DATA_DIR, s['txn_id']))})"):
            for c in ops.get_comments(DATA_DIR, s["txn_id"]):
                st.markdown(f"**{c['author']}**  ·  {c['ts']}  \n{c['text']}")
            new_comment = st.text_area("Add a comment", key=f"comment_{s['txn_id']}",
                                       label_visibility="collapsed", placeholder="Add a comment…")
            if st.button("Post comment", key=f"post_comment_{s['txn_id']}") and new_comment:
                ops.add_comment(DATA_DIR, s["txn_id"], CURRENT_USER, new_comment)
                st.rerun()

        with st.expander("Audit trail and replay"):
            log_entries = entries_for_txn(AUDIT_PATH, s["txn_id"])
            if log_entries:
                latest = log_entries[-1]
                st.json(latest, expanded=False)
                if st.button("Verify replay", key=f"replay_{s['txn_id']}"):
                    res = replay_txn(s["txn_id"], INV_CSV, SETTLE_CSV, use_llm=use_llm, logged_entry=latest)
                    (st.success if res.get("matches") else st.warning)(res["reason"])
            else:
                st.caption("No audit entry yet.")

        is_human_confirmed = s["txn_id"] in st.session_state["confirmed"]
        is_rejected = s["txn_id"] in st.session_state["rejected"]
        not_ledger_final = is_human_confirmed or is_rejected  # deterministic matches are never undoable here
        if not_ledger_final and not locked:
            undo_label = "Undo confirm" if is_human_confirmed else "Undo reject"
            if st.button(f"{undo_label}", key=f"undo_{s['txn_id']}", icon=":material/undo:"):
                st.session_state["confirmed"].discard(s["txn_id"])
                st.session_state["rejected"].discard(s["txn_id"])
                st.rerun()
        if is_human_confirmed:
            if st.button("Post to simulated ERP", key=f"post_{s['txn_id']}", width="stretch"):
                log_entries = entries_for_txn(AUDIT_PATH, s["txn_id"])
                ahash = log_entries[-1]["hash"] if log_entries else "unlogged"
                for inv_id in s["matched_invoice_ids"]:
                    ops.post_to_erp(DATA_DIR, inv_id, s["txn_id"], info.get("amount", 0), ahash)
                st.success("Posted — see Settlements → Posted entries.")

        if allow_escalate and not is_rejected:
            if st.button("Escalate", key=f"escalate_{s['txn_id']}", icon=":material/priority_high:"):
                st.session_state.setdefault("manually_escalated", set()).add(s["txn_id"])
                st.toast("Assigned to review queue.", icon=":material/check_circle:")

    def render_row(s: dict, *, tab_key: str, allow_escalate: bool = False):
        is_animating = s["txn_id"] in confirm_anim_ids
        row_key = f"row2_{tab_key}_{s['txn_id']}"
        if is_animating:
            delay = confirm_anim_ids.index(s["txn_id"]) * motion.STAGGER_MS
            st.markdown(
                f"<style>.st-key-{row_key} {{ animation: rp-row-pulse {motion.PULSE_TOTAL_MS}ms "
                f"ease-out {delay}ms, rp-row-collapse {motion.COLLAPSE_MS}ms ease-out "
                f"{motion.PULSE_TOTAL_MS + delay}ms forwards; }}</style>", unsafe_allow_html=True)

        with st.container(key=row_key):
            c0, c1, c2, c3, c4 = st.columns([0.35, 1.6, 1.6, 1.6, 1.4])
            info = enriched.get(s["txn_id"], {})
            with c0:
                # item 6: per-row bulk-select checkbox, only meaningful on the
                # Pending confirmation tab -- rendered as a fixed-width empty
                # slot elsewhere so column alignment stays identical across tabs.
                if tab_key == "pending" and not is_animating:
                    checked = st.checkbox("select", key=f"bulksel_{row_key}",
                                          value=s["txn_id"] in st.session_state["bulk_selected"],
                                          label_visibility="collapsed")
                    if checked:
                        st.session_state["bulk_selected"].add(s["txn_id"])
                    else:
                        st.session_state["bulk_selected"].discard(s["txn_id"])
            with c1:
                st.markdown(f"**{s['txn_id']}**")
                if info.get("amount") is not None:
                    st.markdown(f"<span class='rp-amount' style='font-size:12.5px;color:#6B7280'>"
                               f"{fmt_inr(info['amount'])}</span>", unsafe_allow_html=True)
            with c2:
                if is_animating:
                    st.markdown(
                        f'<span class="rp-badge-wrap"><span class="rp-badge-old">{status_row(s["status"])}'
                        f'</span><span class="rp-badge-new">{status_row("matched")}</span></span>',
                        unsafe_allow_html=True)
                else:
                    st.markdown(confidence_pill(s["confidence"]), unsafe_allow_html=True)
            with c3:
                st.markdown(autonomy_badge(autonomy_label_for(s)), unsafe_allow_html=True)
            with c4:
                if not is_animating:
                    if st.button("Details", key=f"toggle_{row_key}", width="stretch", type="tertiary"):
                        st.session_state.setdefault("expanded_rows", set())
                        if s["txn_id"] in st.session_state["expanded_rows"]:
                            st.session_state["expanded_rows"].discard(s["txn_id"])
                        else:
                            st.session_state["expanded_rows"].add(s["txn_id"])
                        st.rerun()

            if not is_animating and s["txn_id"] in st.session_state.get("expanded_rows", set()):
                with st.container():
                    render_expand(s, allow_escalate=allow_escalate)
                    if s["status"] == "pending_confirmation" and tab_key == "pending" and not locked \
                            and s["txn_id"] not in st.session_state["confirmed"] \
                            and s["txn_id"] not in st.session_state["rejected"]:
                        ac1, ac2 = st.columns(2)
                        if ac1.button("Accept", type="primary", width="stretch", key=f"acc2_{s['txn_id']}"):
                            start_confirm_animation([s["txn_id"]], {s["txn_id"]: s["layer"]})
                        if ac2.button("Reject", width="stretch", key=f"rej2_{s['txn_id']}"):
                            st.session_state["rejected"].add(s["txn_id"])
                            rules_mod.record_override(DATA_DIR, s["layer"], accepted=False)
                            st.rerun()
        st.session_state["seen_txn_ids"].add(s["txn_id"])

    tab_auto, tab_pending, tab_exc, tab_ledger = st.tabs(
        ["Auto-matched", "Pending confirmation", "Exceptions", "Ledger"])

    with tab_auto:
        rows = [s for s in settlements if effective_status(s) == "matched" and _layer_ok(s) and _search_ok(s) and _band_ok(s)]
        st.caption(f"{len(rows)} matched")
        for s in rows:
            render_row(s, tab_key="auto")
        if not rows:
            st.markdown('<p class="rp-empty">Nothing matched at this filter.</p>', unsafe_allow_html=True)

    with tab_pending:
        pending_rows = [s for s in settlements
                       if (effective_status(s) == "pending_confirmation" and _layer_ok(s) and _search_ok(s) and _band_ok(s))
                       or s["txn_id"] in confirm_anim_ids]
        st.caption(f"{len(pending_rows)} pending")

        unanimated_pending = [s for s in pending_rows if s["txn_id"] not in confirm_anim_ids]

        # ---- item 6: bulk actions -- checkbox per row (in render_row) plus
        # one "Confirm selected" button above the table that confirms every
        # checked row in a single click.
        pending_ids = {s["txn_id"] for s in unanimated_pending}
        selected_here = st.session_state["bulk_selected"] & pending_ids
        if selected_here:
            bc1, bc2 = st.columns([4, 1.3])
            bc1.markdown(f"**{len(selected_here)} selected**")
            if bc2.button(f"Confirm selected ({len(selected_here)})", key="bulk_confirm_pending",
                         type="primary", width="stretch"):
                if locked:
                    st.error("This run is certified and locked.")
                else:
                    layer_by_txn = {t: next(s["layer"] for s in unanimated_pending if s["txn_id"] == t)
                                    for t in selected_here}
                    st.session_state["bulk_selected"] -= selected_here
                    start_confirm_animation(list(selected_here), layer_by_txn)
            st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
        groups = anomaly.group_for_bulk_approval(unanimated_pending)
        if groups and len(unanimated_pending) > 1:
            st.markdown("**Grouped approval**")
            for g in groups[:5]:
                gc = st.columns([5, 1])
                gc[0].markdown(f"`{g['layer']}`  ·  {g['deduction_label']} — "
                              f"{g['count']} items, avg confidence {g['avg_confidence']}")
                if gc[1].button(f"Approve all {g['count']}",
                               key=f"bulkapprove2_{g['layer']}_{g['deduction_label']}"):
                    if locked:
                        st.error("This run is certified and locked.")
                    else:
                        start_confirm_animation(g["txn_ids"], {t: g["layer"] for t in g["txn_ids"]})
            st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)

        for s in pending_rows:
            render_row(s, tab_key="pending")
        if not pending_rows:
            st.markdown('<p class="rp-empty">Nothing pending at this filter.</p>', unsafe_allow_html=True)

        if confirm_anim:
            n = len(confirm_anim_ids)
            hold_s = (max((n - 1) * motion.STAGGER_MS + motion.PULSE_TOTAL_MS + motion.COLLAPSE_MS, 500) + 150) / 1000
            time.sleep(hold_s)
            for t in confirm_anim_ids:
                st.session_state["confirmed"].add(t)
                rules_mod.record_override(DATA_DIR, confirm_anim["layer_by_txn"].get(t, "unknown"), accepted=True)
            st.session_state["confirm_anim"] = None
            st.rerun()

    with tab_exc:
        rows = [s for s in settlements if effective_status(s) == "exception" and _layer_ok(s) and _search_ok(s) and _band_ok(s)]
        st.caption(f"{len(rows)} exceptions")
        for s in rows:
            render_row(s, tab_key="exception", allow_escalate=True)
        if not rows:
            st.markdown('<p class="rp-empty">No exceptions at this filter.</p>', unsafe_allow_html=True)

    with tab_ledger:
        inv_rows = [{"Invoice ID": i["invoice_id"], "Customer": i["customer"],
                    "Invoice date": i["invoice_date"], "Amount": fmt_inr(i["amount"]),
                    "Remaining": fmt_inr(i["remaining_amount"]), "Status": i["status"],
                    "Reference code": i["reference_code"]} for i in invoices]
        st.dataframe(pd.DataFrame(inv_rows), width="stretch", hide_index=True)

    # ---- Activity log drawer, pinned, slides up 200ms ---------------------
    st.session_state.setdefault("activity_open", False)
    toggle_label = "Activity log  ▲" if st.session_state["activity_open"] else "Activity log  ▼"
    if st.button(toggle_label, key="activity_toggle", width="stretch", type="tertiary"):
        st.session_state["activity_open"] = not st.session_state["activity_open"]
        st.rerun()

    max_h = "480px" if st.session_state["activity_open"] else "0px"
    st.markdown(f"<style>.st-key-activity_drawer {{ max-height: {max_h}; }}</style>",
               unsafe_allow_html=True)
    with st.container(key="activity_drawer"):
        st.markdown(
            '<div class="rp-drawer-row head"><span>Time</span><span>Action</span>'
            '<span>Layer</span><span>Txn ID</span></div>', unsafe_allow_html=True)
        log_entries = load_chain(AUDIT_PATH) if os.path.exists(AUDIT_PATH) else []
        for e in reversed(log_entries[-40:]):
            st.markdown(
                f'<div class="rp-drawer-row"><span>{e["ts"][11:19]}</span>'
                f'<span>{e["output"]["status"]}</span><span>{e["rule_invoked"]}</span>'
                f'<span class="rp-mono">{e["inputs"].get("txn_id", "—")}</span></div>',
                unsafe_allow_html=True)
        if not log_entries:
            st.markdown('<p class="rp-empty">No activity logged yet.</p>', unsafe_allow_html=True)

elif active_tab == "A/B: LLM impact":
    from recon_agent.matcher import (ALL_LAYERS, compute_metrics, load_deduction_truth,
                                     load_ground_truth, load_invoices, load_settlements as _load_s,
                                     reconcile as _reconcile)

    st.markdown("**A/B: does the LLM investigator earn its place?**")
    st.caption("Runs the exact same batch (same seed, same data on disk) through the pipeline "
              "twice — once with the Tier-1 investigator enabled, once force-disabled (everything "
              "layers 1-4 couldn't resolve goes straight to exception, no LLM call at all).")

    if st.button("Run A/B comparison on this batch", key="ab_run", type="primary"):
        with st.spinner("Reconciling with L5 enabled, then with L5 disabled…"):
            invoices_a = load_invoices(INV_CSV)
            invoices_b = load_invoices(INV_CSV)  # fresh copy -- reconcile() mutates in place
            settlements_raw = _load_s(SETTLE_CSV)
            gt_path, ded_path = GT_CSV, DED_CSV
            gt = load_ground_truth(gt_path) if os.path.exists(gt_path) else None
            ded_truth = load_deduction_truth(ded_path) if os.path.exists(ded_path) else None

            results_with_llm = _reconcile(invoices_a, list(settlements_raw), use_llm=use_llm,
                                          enabled_layers=ALL_LAYERS)
            results_without_llm = _reconcile(invoices_b, list(settlements_raw), use_llm=use_llm,
                                             enabled_layers=ALL_LAYERS - {"L5"})

            metrics_with = compute_metrics(results_with_llm, gt, ded_truth)
            metrics_without = compute_metrics(results_without_llm, gt, ded_truth)

            st.session_state["ab_result"] = {
                "with_llm": {"results": results_with_llm, "metrics": metrics_with},
                "without_llm": {"results": results_without_llm, "metrics": metrics_without},
            }

    if st.session_state.get("ab_result"):
        ab = st.session_state["ab_result"]
        mw, mwo = ab["with_llm"]["metrics"], ab["without_llm"]["metrics"]

        cmp_rows = []
        for label, key, is_pct in [
            ("Auto-match rate", "auto_match_rate", True), ("Resolved rate", "resolved_rate", True),
            ("Precision", "precision", True), ("Recall", "recall", True),
            ("Exceptions", "exception", False),
        ]:
            vw, vwo = mw.get(key), mwo.get(key)
            fmt = (lambda v: f"{v*100:.1f}%" if v is not None else "n/a") if is_pct else (lambda v: str(v))
            cmp_rows.append({"Metric": label, "With LLM (L5 on)": fmt(vw),
                            "Without LLM (L5 off)": fmt(vwo)})
        st.dataframe(pd.DataFrame(cmp_rows), width="stretch", hide_index=True)

        # ---- the one clear sentence: net LLM contribution, honestly scored --
        results_with = ab["with_llm"]["results"]
        results_without = ab["without_llm"]["results"]
        by_txn_without = {r["txn_id"]: r for r in results_without}

        gt_path = GT_CSV
        gt = load_ground_truth(gt_path) if os.path.exists(gt_path) else None

        newly_correct = 0
        false_positives_introduced = 0
        if gt is not None:
            for r in results_with:
                was_exception = by_txn_without.get(r["txn_id"], {}).get("status") == "exception"
                if not was_exception:
                    continue  # L1-4 already resolved this one identically in both runs
                if r["status"] in ("matched", "pending_confirmation"):
                    predicted = set(r["matched_invoice_ids"])
                    true = gt.get(r["txn_id"], set())
                    if predicted and predicted == true:
                        newly_correct += 1
                    elif predicted:
                        false_positives_introduced += 1

            st.markdown(
                f"**The LLM investigator correctly resolved {newly_correct} additional record(s)** "
                f"that layers 1-4 alone left as exceptions"
                + (f", and introduced **{false_positives_introduced} false positive(s)** it "
                   f"wouldn't have made deterministically." if false_positives_introduced
                   else ", with **zero false positives** introduced relative to the deterministic-only run.")
            )
        else:
            st.markdown('<p class="rp-empty">No ground truth file for this batch — can\'t score '
                      'correctness of the LLM\'s additional resolutions.</p>', unsafe_allow_html=True)
    else:
        st.markdown('<p class="rp-empty">Not run yet this session.</p>', unsafe_allow_html=True)

elif active_tab == "Settlements":
    t1, t2, t3 = st.tabs(["Auto-matched", "Invoice ledger", "Posted entries"])
    with t1:
        matched = [s for s in settlements if effective_status(s) == "matched"]
        if matched:
            rows = [{"Txn ID": s["txn_id"], "Invoices": ", ".join(s["matched_invoice_ids"]),
                    "Layer": s["layer"], "Deduction": s.get("deduction_label") or "—",
                    "Confidence": s["confidence"]} for s in matched]
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                        column_config={"Confidence": st.column_config.ProgressColumn(
                            "Confidence", min_value=0, max_value=100)})
        else:
            st.markdown('<p class="rp-empty">Nothing auto-matched yet.</p>', unsafe_allow_html=True)
    with t2:
        inv_rows = [{"Invoice ID": i["invoice_id"], "Customer": i["customer"],
                    "Invoice date": i["invoice_date"], "Amount": fmt_inr(i["amount"]),
                    "Remaining": fmt_inr(i["remaining_amount"]), "Status": i["status"],
                    "Reference code": i["reference_code"]} for i in invoices]
        st.dataframe(pd.DataFrame(inv_rows), width="stretch", hide_index=True)
    with t3:
        posted = ops.load_posted_entries(DATA_DIR)
        if posted:
            st.dataframe(pd.DataFrame(posted), width="stretch", hide_index=True)
        else:
            st.markdown('<p class="rp-empty">No simulated ERP postings yet.</p>', unsafe_allow_html=True)

# ==========================================================================
# REPORTS
# ==========================================================================
elif active_tab == "Model diagnostics":
    import evaluate as _eval

    gt = None
    if os.path.exists(GT_CSV):
        from recon_agent.matcher import load_ground_truth
        gt = load_ground_truth(GT_CSV)

    # ---- (a) precision vs. auto-approval-rate, draggable threshold --------
    st.markdown("**Precision vs. auto-approval rate**")
    st.caption("Sweeps the Tier-1 investigator's confidence threshold; the slider is the "
              "draggable marker — KPIs below recompute live as you move it.")
    if gt:
        curve = rules_mod.threshold_sweep(settlements, gt)
        if any(c["n"] > 0 for c in curve):
            cdf = pd.DataFrame(curve).set_index("tau")
            st.line_chart(cdf[["coverage", "precision"]], color=["#0D94FB", "#16A34A"])
            tau_pick = st.slider("Confidence threshold", 0, 100, 85, step=5, key="diag_tau")
            row = min(curve, key=lambda r: abs(r["tau"] - tau_pick))
            c1, c2, c3 = st.columns(3)
            c1.metric("Auto-approval rate @ this threshold", f"{row['coverage']*100:.1f}%")
            c2.metric("Precision @ this threshold", f"{row['precision']*100:.1f}%")
            c3.metric("n accepted", row["n"])
        else:
            st.markdown('<p class="rp-empty">Not enough L5 volume in this batch to chart a curve.</p>',
                       unsafe_allow_html=True)
    else:
        st.markdown('<p class="rp-empty">No ground truth file for this batch.</p>', unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)

    # ---- (b) per-layer ablation, pulled from evaluate.py -------------------
    st.markdown("**Per-layer ablation**")
    st.caption("Each layer disabled in turn, on this exact batch — a real rerun via evaluate.py, "
              "not a canned chart. Always deterministic (no live LLM calls), regardless of the "
              "sidebar toggle: this measures the matcher's own layer-by-layer contribution, and "
              "6 reruns' worth of live calls would make it slow without changing what it's "
              "actually measuring.")
    if st.button("Run ablation on this batch", key="diag_run_ablation"):
        with st.spinner("Reconciling with each layer disabled in turn…"):
            st.session_state["diag_ablation"] = _eval.ablation(DATA_DIR, use_llm=False)
    if st.session_state.get("diag_ablation"):
        abl = st.session_state["diag_ablation"]
        abl_df = pd.DataFrame({"auto_match_rate": {k: v["auto_match_rate"] for k, v in abl.items()}})
        st.bar_chart(abl_df, color="#0D94FB")
        st.dataframe(pd.DataFrame([
            {"Configuration": k, "Auto-match rate": v["auto_match_rate"],
            "Precision": v.get("precision"), "Recall": v.get("recall")}
            for k, v in abl.items()
        ]), width="stretch", hide_index=True)
    else:
        st.markdown('<p class="rp-empty">Not run yet this session.</p>', unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)

    # ---- (c) mean +/- std across 5 seeds -----------------------------------
    st.markdown("**Mean ± std across 5 seeds**")
    st.caption("The demo batch alone is one seed — this reruns the full pipeline on 5 freshly "
              "generated seeds at the current batch size so the headline numbers aren't read "
              "off a single lucky (or unlucky) run. Always deterministic (no live LLM calls) — "
              "5 fresh synthetic batches' worth of live calls would be slow and would measure "
              "API variance, not matcher quality.")
    if st.button("Run 5-seed evaluation", key="diag_run_multiseed"):
        import tempfile
        with st.spinner("Generating and reconciling 5 seeds…"):
            n_settlements_guess = metrics["total_settlements"]
            with tempfile.TemporaryDirectory(prefix="recon_diag_") as scratch_dir:
                st.session_state["diag_multiseed"] = _eval.multi_seed(
                    scratch_dir, [101, 102, 103, 104, 105], n_settlements_guess, use_llm=False)
    if st.session_state.get("diag_multiseed"):
        ms = st.session_state["diag_multiseed"]
        mcols = st.columns(4)
        for col, key, label in zip(mcols,
                                   ["auto_match_rate", "precision", "recall", "deduction_hypothesis_accuracy"],
                                   ["Auto-match rate", "Precision", "Recall", "Deduction accuracy"]):
            v = ms[key]
            col.metric(label, f"{v['mean']*100:.1f}%" if v["mean"] is not None else "n/a",
                      delta=f"± {v['std']*100:.1f}pp" if v["std"] is not None else None,
                      delta_color="off")
        st.caption(f"Honeypots across these 5 seeds: {ms['honeypots']['baited']} of "
                  f"{ms['honeypots']['total']} baited.")
    else:
        st.markdown('<p class="rp-empty">Not run yet this session.</p>', unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)

    # ---- (item 9) honeypots ------------------------------------------------
    st.markdown("**Honeypots**")
    hp_path = os.path.join(DATA_DIR, "honeypots.csv")
    if os.path.exists(hp_path):
        honeypot_ids = _eval.load_honeypots(hp_path)
        results_by_txn = {s["txn_id"]: s for s in settlements}
        baited = [t for t in honeypot_ids
                 if results_by_txn.get(t, {}).get("status") in ("matched", "pending_confirmation")
                 and results_by_txn[t]["matched_invoice_ids"]]
        declined = honeypot_ids - set(baited)
        c1, c2 = st.columns(2)
        c1.metric("Baited (agent wrongly matched)", f"{len(baited)} of {len(honeypot_ids)}")
        c2.metric("Correctly declined", f"{len(declined)} of {len(honeypot_ids)}")
        if baited:
            st.warning("These adversarial credits (same amount as a real invoice, different "
                      "counterparty, no reference code) were wrongly matched. Every one baited "
                      "here cleared the confidence bar on amount+date closeness alone, with no "
                      "genuine reference-text corroboration — a real gap in the rule-based "
                      "fallback's confidence scoring, not fixed as part of this diagnostics view.")
            for t in baited:
                s = results_by_txn[t]
                st.markdown(f"- `{t}` → wrongly matched to {', '.join(s['matched_invoice_ids'])} "
                          f"(confidence {s['confidence']}, layer `{s['layer']}`)")
        if declined:
            with st.expander(f"Correctly declined ({len(declined)})"):
                for t in declined:
                    st.markdown(f"- `{t}`")
    else:
        st.markdown('<p class="rp-empty">No honeypots.csv for this batch — regenerate to get one '
                  '(added after this batch was first generated).</p>', unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)

    # ---- (items 2, 3): calibration / reliability diagram + confidence-vs-
    # outcome scatter. Scoped to Tier-1 investigator proposals only (layer 5)
    # -- deterministic layers 1-4 always report confidence=100 by
    # construction, which would swamp any calibration analysis with a trivial
    # spike and tell you nothing about whether confidence SCORES are real.
    st.markdown("**Calibration: does confidence match reality?**")
    st.caption("Scoped to Tier-1 investigator proposals (layer 5) only — deterministic layers "
              "always report 100% by construction and would swamp this otherwise.")
    if gt:
        proposals = [s for s in settlements if s["layer"] == "llm_investigator"
                    and s["status"] == "pending_confirmation"]
        if proposals:
            def _is_correct(p: dict) -> bool:
                return bool(p["matched_invoice_ids"]) and set(p["matched_invoice_ids"]) == gt.get(p["txn_id"], set())

            bin_edges = list(range(0, 101, 10))
            bin_rows = []
            n_total = len(proposals)
            ece = 0.0
            for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
                in_bin = [p for p in proposals if lo <= p["confidence"] < hi
                         or (hi == 100 and p["confidence"] == 100)]
                if not in_bin:
                    continue
                correct = sum(1 for p in in_bin if _is_correct(p))
                acc = correct / len(in_bin)
                avg_conf = sum(p["confidence"] for p in in_bin) / len(in_bin)
                bin_rows.append({"Predicted confidence": avg_conf, "Actual accuracy": acc, "n": len(in_bin)})
                ece += (len(in_bin) / n_total) * abs(acc - avg_conf / 100)

            st.metric("Expected Calibration Error (ECE)", f"{ece:.4f}",
                     help="Lower is better -- 0 means predicted confidence exactly matched "
                          "actual accuracy in every bin. Bucket sizes are shown in the table "
                          "below, since ECE alone can look good with very few proposals per bin.")

            if bin_rows:
                import altair as alt
                cal_df = pd.DataFrame(bin_rows)
                line = alt.Chart(cal_df).mark_line(point=True, color="#0D94FB").encode(
                    x=alt.X("Predicted confidence:Q", scale=alt.Scale(domain=[0, 100])),
                    y=alt.Y("Actual accuracy:Q", scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(format="%")),
                    tooltip=["Predicted confidence", "Actual accuracy", "n"],
                )
                diagonal = alt.Chart(pd.DataFrame({"x": [0, 100], "y": [0, 1]})).mark_line(
                    strokeDash=[4, 4], color="#6B7280").encode(x="x:Q", y="y:Q")
                st.altair_chart((line + diagonal).properties(height=220), width="stretch")
                st.dataframe(cal_df, width="stretch", hide_index=True)

            # ---- item 3: confidence-vs-outcome scatter -------------------
            st.markdown("**Confidence vs. outcome**")
            import random as _random
            rng = _random.Random(42)  # fixed seed -- jitter must be reproducible, not flicker on rerun
            scatter_rows = [{
                "Confidence": p["confidence"],
                "Outcome": "Correct" if _is_correct(p) else "Incorrect",
                "y": (1 if _is_correct(p) else 0) + rng.uniform(-0.12, 0.12),
            } for p in proposals]
            scatter_df = pd.DataFrame(scatter_rows)
            scatter = alt.Chart(scatter_df).mark_circle(size=70, opacity=0.65).encode(
                x=alt.X("Confidence:Q", scale=alt.Scale(domain=[0, 100])),
                y=alt.Y("y:Q", title="Outcome", axis=alt.Axis(
                    values=[0, 1], labelExpr="datum.value == 0 ? 'Incorrect' : 'Correct'")),
                color=alt.Color("Outcome:N", scale=alt.Scale(
                    domain=["Correct", "Incorrect"], range=["#16A34A", "#DC2626"])),
                tooltip=["Confidence", "Outcome"],
            )
            st.altair_chart(scatter.properties(height=200), width="stretch")
        else:
            st.markdown('<p class="rp-empty">No Tier-1 investigator proposals in this run to '
                      'calibrate.</p>', unsafe_allow_html=True)
    else:
        st.markdown('<p class="rp-empty">No ground truth file for this batch.</p>', unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)

    # ---- (item 10) verify determinism --------------------------------------
    st.markdown("**Verify determinism**")
    st.caption("Reruns this exact batch N times on identical input and reports the fraction of "
              "decisions that landed the same way every time (DecDet) — the audit-replay story, "
              "made clickable.")
    reps = st.slider("Reruns", 2, 10, 5, key="diag_decdet_reps")
    if st.button("Verify determinism", key="diag_run_decdet"):
        with st.spinner(f"Rerunning {reps} times…"):
            st.session_state["diag_decdet"] = _eval.decision_determinism(DATA_DIR, use_llm=use_llm, reps=reps)
    if st.session_state.get("diag_decdet") is not None:
        val = st.session_state["diag_decdet"]
        (st.success if val >= 0.95 else st.warning)(
            f"DecDet: {val*100:.1f}% of decisions were identical across {reps} reruns."
            + ("" if not use_llm else " (a live LLM call is not seeded, so <100% here reflects "
                                      "the model's own consistency, not a bug.)"))

elif active_tab == "Reports":
    gt = None
    if os.path.exists(GT_CSV):
        from recon_agent.matcher import load_ground_truth
        gt = load_ground_truth(GT_CSV)

    st.markdown("**Rule preview**")
    st.caption("Backtest a rule's current autonomy setting against hidden ground truth before trusting it.")
    if gt:
        for layer, rule in rules.items():
            if not rule.tunable:
                continue
            rc1, rc2 = st.columns([4, 1])
            rc1.markdown(f"**{rule.label}** — autonomy: `{rule.autonomy}`"
                        f"{f' at confidence ≥ {rule.threshold}' if rule.autonomy=='auto_under_threshold' else ''}")
            show_dryrun = rc2.button("Preview", key=f"dryrun_{layer}", width="stretch")
            if show_dryrun:
                st.session_state[f"dryrun_shown_{layer}"] = True
                st.session_state[f"dryrun_animate_{layer}"] = True
            if st.session_state.get(f"dryrun_shown_{layer}"):
                dr = rules_mod.dry_run(rule, settlements, gt)
                # ---- #7: would-match/would-mismatch/would-auto-close count up together ----
                if st.session_state.get(f"dryrun_animate_{layer}"):
                    motion.count_up_grid([
                        {"label": "Candidates", "value": dr["candidates"], "decimals": 0},
                        {"label": "Would auto-close", "value": dr["would_auto_close"], "decimals": 0},
                        {"label": "Would match", "value": dr["would_match"], "decimals": 0},
                        {"label": "Would mis-match", "value": dr["would_mismatch"], "decimals": 0},
                    ])
                    st.session_state[f"dryrun_animate_{layer}"] = False
                else:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Candidates", dr["candidates"])
                    c2.metric("Would auto-close", dr["would_auto_close"])
                    c3.metric("Would match", dr["would_match"])
                    c4.metric("Would mis-match", dr["would_mismatch"])
                if dr["would_be_precision"] is not None:
                    st.caption(f"Backtested precision if live: {dr['would_be_precision']*100:.1f}%")
    else:
        st.markdown('<p class="rp-empty">No ground truth file for this batch.</p>', unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**Confidence threshold — coverage and precision**")
    if gt:
        curve = rules_mod.threshold_sweep(settlements, gt)
        cdf = pd.DataFrame(curve).set_index("tau")
        st.line_chart(cdf[["coverage", "precision"]], color=["#0D94FB", "#16A34A"])
        tau_pick = st.slider("Confidence threshold", 0, 100, 85, step=5)
        row = min(curve, key=lambda r: abs(r["tau"] - tau_pick))
        st.caption(f"At {row['tau']}: coverage {row['coverage']*100:.1f}%, "
                  f"precision {row['precision']*100:.1f}%, n={row['n']}")

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**Rule effectiveness**")
    stats = rules_mod.load_rule_stats(DATA_DIR)
    if stats:
        rows = [{"Layer": k, "Accepted": v["accepted"], "Overridden": v["overridden"],
                "Accept streak": v["streak"]} for k, v in stats.items()]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        for layer in rules_mod.promotion_candidates(DATA_DIR, rules):
            st.success(f"`{layer}` has {rules_mod.PROMOTION_STREAK}+ consecutive accepted suggestions "
                      f"with zero overrides — consider promoting its autonomy in Settings.")
    else:
        st.markdown('<p class="rp-empty">No confirm/reject actions recorded yet.</p>', unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**Audit log**")
    if os.path.exists(AUDIT_PATH):
        ok, msg = verify_chain(AUDIT_PATH)
        (st.success if ok else st.error)(f"Hash chain: {msg}")
    if st.button("Build audit export"):
        data = ops.build_audit_package(DATA_DIR, AUDIT_PATH)
        st.download_button("Download audit_package.zip", data,
                           file_name="audit_package.zip", mime="application/zip")

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**Export**")
    full_rows = [{"Txn ID": s["txn_id"], "Invoices": ", ".join(s["matched_invoice_ids"]),
                 "Status": effective_status(s), "Confidence": s["confidence"], "Layer": s["layer"],
                 "Deduction": s.get("deduction_label") or "—", "Rationale": s["rationale"]}
                for s in settlements]
    ec1, ec2 = st.columns(2)
    with ec1:
        st.download_button("Download full reconciliation report (CSV)",
                           pd.DataFrame(full_rows).to_csv(index=False),
                           file_name="reconciliation_report.csv", mime="text/csv")
    with ec2:
        if os.path.exists(AUDIT_PATH):
            with open(AUDIT_PATH, "rb") as f:
                st.download_button("Download audit log (JSONL)", f.read(),
                                  file_name="audit_log.jsonl", mime="application/x-ndjson")
        else:
            st.caption("No audit log yet for this run.")
    with st.expander("Raw metrics (JSON)"):
        st.json(metrics)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**PDF scorecard**")
    st.caption("One-page PDF: the honest multi-seed table (mean ± std, not this single demo run), "
              "the precision/coverage tradeoff curve, and a one-paragraph architecture summary.")
    if st.button("Build PDF scorecard", key="build_pdf"):
        with st.spinner("Running a 5-seed evaluation and rendering the PDF…"):
            import tempfile

            import evaluate as _eval
            from recon_agent.pdf_report import build_scorecard_pdf

            with tempfile.TemporaryDirectory(prefix="recon_pdf_") as scratch_dir:
                # Always deterministic here, regardless of the sidebar toggle:
                # this scorecard exists to prove the matcher's own precision/
                # recall, and a 5-seed sweep is 5x the settlements of one
                # run -- with use_llm=True that's enough live calls to burn
                # a free-tier daily quota (20/day) in a single button click.
                pdf_summary = _eval.multi_seed(scratch_dir, [201, 202, 203, 204, 205],
                                               metrics["total_settlements"], use_llm=False)
            if gt:
                pdf_summary["threshold_curve"] = rules_mod.threshold_sweep(settlements, gt)
            hp_path = os.path.join(DATA_DIR, "honeypots.csv")
            if os.path.exists(hp_path):
                pdf_summary["honeypots"] = pdf_summary.get("honeypots", {"total": 0, "baited": 0})
            st.session_state["pdf_bytes"] = build_scorecard_pdf(pdf_summary)
    if st.session_state.get("pdf_bytes"):
        st.download_button("Download scorecard.pdf", st.session_state["pdf_bytes"],
                           file_name="scorecard.pdf", mime="application/pdf")

# ==========================================================================
# SETTINGS
# ==========================================================================
elif active_tab == "Settings":
    st.markdown("**Approval chain**")
    cert = ops.get_certification(DATA_DIR, run_id)
    if cert:
        st.caption(f"Prepared by {cert['prepared_by']} at {cert['prepared_at']}")
        if cert["certified"]:
            st.markdown(f"<span class='rp-status-row'>{icon('lock', 14)}&nbsp;Certified by "
                       f"{cert['reviewed_by']} at {cert['reviewed_at']} — this run is locked.</span>",
                       unsafe_allow_html=True)
        else:
            reviewer = st.selectbox("Reviewer (must differ from preparer)",
                                    [o for o in ops.OWNERS if o != "Unassigned"])
            if st.button("Certify this run", type="primary"):
                res = ops.certify_run(DATA_DIR, run_id, reviewer)
                (st.success if res["ok"] else st.error)(res.get("reason", "Certified."))
                if res["ok"]:
                    st.rerun()
    else:
        st.markdown('<p class="rp-empty">Run reconciliation to start a certification chain.</p>',
                   unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**Autonomy per rule**")
    st.dataframe(pd.DataFrame([{"Layer": r.layer, "Label": r.label, "Autonomy": r.autonomy,
                               "Threshold": r.threshold if r.autonomy == "auto_under_threshold" else "—",
                               "Tunable": r.tunable} for r in rules.values()]),
                width="stretch", hide_index=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**Saved views**")
    views = ops.list_saved_views(DATA_DIR)
    if views:
        for name in list(views.keys()):
            vc = st.columns([4, 1])
            vc[0].write(f"**{name}**: {views[name]}")
            if vc[1].button("Delete", key=f"destructive_delview_{name}"):
                ops.delete_view(DATA_DIR, name)
                st.rerun()
    else:
        st.markdown('<p class="rp-empty">No saved views yet.</p>', unsafe_allow_html=True)

    st.markdown('<hr class="rp-divider"/>', unsafe_allow_html=True)
    st.markdown("**Motion and platform-limit scope notes**")
    st.caption(
        "Every animation in this console (see recon_agent/motion.py) is built to communicate "
        "a state change, respects prefers-reduced-motion, and stays under 400ms except the two "
        "the spec itself defines longer: the confirm pulse-and-collapse (500ms + 200ms) and the "
        "pipeline ticker (capped at 2.5s). Two things stay out of reach of a Python-only "
        "Streamlit app regardless: a global keyboard-shortcut layer and a true OS-level command "
        "palette overlay both need a custom bidirectional JS component; what's shipped instead "
        "is Prev/Next controls and a button-triggered command-search popover."
    )
