"""Persistent floating AI assistant widget for the Streamlit console.

Same visual system as the rest of the app (Prussian Blue / Dodger Blue,
150-250ms ease-out, no bounce/spring). The floating button and its panel are
both real Streamlit widgets positioned via `position: fixed`, targeted
through Streamlit's documented `key="..."` -> `.st-key-<key>` CSS class
convention (see ui_theme.py's module docstring for why this is the correct
mechanism here, not a workaround).

Context-aware suggested chips: the caller passes `page_context` plus
whatever real data that context needs — the chips are built from actual
run data (e.g. a real exception's txn_id), never filler placeholders.

Answers go through `recon_agent.assistant.ask()` — the same bounded,
read-only Q&A used elsewhere, so "powered by the same agent, read-only" is
a true statement, not copy.
"""
from __future__ import annotations

import re

import streamlit as st

from . import assistant

CSS = """
<style>
.st-key-chat_fab { position: fixed !important; bottom: 24px; right: 24px; z-index: 9998; }
.st-key-chat_fab button {
    width: 56px !important; height: 56px !important; border-radius: 50% !important;
    background: #0D94FB !important; color: #fff !important; border: none !important;
    box-shadow: 0 4px 16px rgba(1,38,82,0.25) !important; transition: background-color 150ms ease-out !important;
}
.st-key-chat_fab button:hover { background: #0B85E0 !important; }
@keyframes rp-chat-pulse {
    0%   { box-shadow: 0 0 0 0 rgba(13,148,251,0.55), 0 4px 16px rgba(1,38,82,0.25); }
    100% { box-shadow: 0 0 0 20px rgba(13,148,251,0), 0 4px 16px rgba(1,38,82,0.25); }
}

.st-key-chat_panel {
    position: fixed !important; bottom: 90px; right: 24px; z-index: 9999; width: 280px;
    background: #fff; border-radius: 16px; border: 1px solid #E5E8EC;
    box-shadow: 0 12px 40px rgba(1,38,82,0.16);
    transition: opacity 200ms ease-out, transform 200ms ease-out, max-height 200ms ease-out;
}
.rp-chat-title { display:flex; align-items:center; gap:7px; font-weight:700; color:#012652; font-size:13.5px; }
.rp-chat-dot { width:7px; height:7px; border-radius:50%; background:#16A34A; flex-shrink:0; }
.rp-chat-msg { background:#F5F7FA; border-radius:10px; padding:7px 10px; font-size:12px; color:#1A1F2B; margin-bottom:6px; line-height:1.4; }
.rp-chat-q { color:#012652; font-weight:600; font-size:12px; margin:8px 0 2px; }
.rp-chat-greet { color:#6B7280; font-size:12px; line-height:1.5; padding:4px 2px 8px; }
.rp-chat-source { font-size:9.5px; color:#6B7280; margin-top:2px; }
.rp-chat-caption { font-size:10px; color:#6B7280; margin-top:2px; }
[class*="st-key-chatchip_"] button {
    border-radius: 999px !important; border: 1px solid #0D94FB !important; background: transparent !important;
    color: #0D94FB !important; font-size: 11px !important; padding: 3px 12px !important; box-shadow:none !important;
}
</style>
"""


def _dashboard_questions(settlements: list[dict]) -> list[str]:
    exc = next((s for s in settlements if s["status"] == "exception"), None)
    q1 = f"Why did {exc['txn_id']} go to exceptions?" if exc else "Why do exceptions happen in this run?"
    return [q1, "What's the difference between precision and recall here?",
           "Show me every pending confirmation under 70% confidence"]


QUICK_PRE_RUN = [
    "What does each of the 5 layers do?",
    "What happens if no API key is set?",
    "How is the synthetic data generated?",
]

# item 18: starter chips change based on which tab is active, not just a
# single generic "dashboard" bucket.
QUICK_BY_TAB = {
    "A/B: LLM impact": [
        "Why isn't the LLM's contribution always positive?",
        "What counts as a false positive here?",
    ],
    "Overview": [
        "Why is the auto-match rate not 100%?",
        "What's the difference between precision and recall here?",
        "How many records are in this run?",
    ],
    "Settlements": [
        "Which invoices are still open?",
        "How does a partial payment stay open across settlements?",
    ],
    "Model diagnostics": [
        "Why were honeypots baited?",
        "What does DecDet mean?",
        "What does the ablation table show?",
    ],
    "Reports": [
        "What is a rule dry-run?",
        "How is the confidence threshold chosen?",
    ],
    "Settings": [
        "What's the difference between auto-confirm and suggest-only?",
    ],
}


# item: the assistant as a control surface, not just Q&A — a small, explicit
# set of natural-language filter requests actually drives the Reconciliation
# tab's real filter widgets (recon_search / recon_band_filter / layer_filter_
# active), rather than only describing the answer in prose. Deliberately
# narrow: it only ever sets state to something backed by real, currently-
# present data (a real layer that appears in this run, a real txn_id), and
# only when the phrasing is unambiguously a "show me" request — never a
# guess dressed up as an action.
_LAYER_KEYWORDS = {
    "partial payment": "exact_reference+partial_payment",
    "subset-sum": "amount_date_subset_sum", "subset sum": "amount_date_subset_sum",
    "batch": "anchored_batch_completion",
    "llm investigator": "llm_investigator", "investigator": "llm_investigator",
    "llm": "llm_investigator",
}
_BAND_KEYWORDS = {
    "high confidence": "High", "low confidence": "Low", "medium confidence": "Medium",
    "needs review": "Medium", "uncertain": "Low",
}
_ACTION_PHRASES = ("show me", "show all", "filter to", "filter for", "find me",
                  "list all", "list every", "which pending", "which settlements",
                  "which exception")


def filter_intent(question: str, settlements: list[dict]) -> dict | None:
    """Returns {band?, layer?, search?} to apply to the Reconciliation tab's
    real filter widgets, or None if this question isn't an unambiguous
    filter request. Exposed at module level so it's independently testable."""
    q = question.lower()
    if not any(p in q for p in _ACTION_PHRASES):
        return None

    intent: dict = {}
    m = re.search(r"\b(?:under|below|less than)\s+(\d+)\s*%", q)
    if m and "confidence" in q:
        thr = int(m.group(1))
        intent["band"] = "Low" if thr <= 55 else ("Medium" if thr <= 80 else "High")
    else:
        for phrase, band in _BAND_KEYWORDS.items():
            if phrase in q:
                intent["band"] = band
                break

    present_layers = {s["layer"] for s in settlements}
    for phrase, layer in _LAYER_KEYWORDS.items():
        if phrase in q and layer in present_layers:
            intent["layer"] = layer
            break

    txn_hit = next((s for s in settlements if s["txn_id"].upper() in question.upper()), None)
    if txn_hit:
        intent["search"] = txn_hit["txn_id"]

    return intent or None


def _chips_for(page_context: str, settlements, focused_txn, active_tab: str | None) -> list[str]:
    if focused_txn:
        return [f"Explain {focused_txn}'s match in plain English", f"What would break {focused_txn}'s match?"]
    if page_context == "pre_run":
        return QUICK_PRE_RUN
    if active_tab and active_tab in QUICK_BY_TAB:
        return QUICK_BY_TAB[active_tab]
    return _dashboard_questions(settlements or [])  # Reconciliation tab, or unrecognized


def render(page_context: str, *, metrics: dict | None = None,
          settlements: list[dict] | None = None, focused_txn: str | None = None,
          active_tab: str | None = None, use_llm: bool = False) -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    st.session_state.setdefault("chat_open", False)
    st.session_state.setdefault("chat_pulsed", False)
    st.session_state.setdefault("widget_chat_log", [])

    # ---- pulse-glow: only injected on the very first render this session --
    if not st.session_state["chat_pulsed"]:
        st.markdown("<style>.st-key-chat_fab button { animation: rp-chat-pulse 2s ease-out 1; }</style>",
                   unsafe_allow_html=True)
        st.session_state["chat_pulsed"] = True

    # ---- slide state: content always in the DOM, visibility via max-height
    if st.session_state["chat_open"]:
        st.markdown("<style>.st-key-chat_panel { opacity:1; transform:translateY(0); "
                   "max-height:520px; pointer-events:auto; overflow-y:auto; padding:14px 14px 10px; }"
                   "</style>", unsafe_allow_html=True)
    else:
        st.markdown("<style>.st-key-chat_panel { opacity:0; transform:translateY(16px); "
                   "max-height:0; pointer-events:none; overflow:hidden; padding:0; border:none; "
                   "box-shadow:none; }</style>", unsafe_allow_html=True)

    with st.container(key="chat_panel"):
        hc1, hc2 = st.columns([5, 1])
        with hc1:
            st.markdown('<div class="rp-chat-title"><span class="rp-chat-dot"></span>'
                       'Ask about this reconciliation</div>', unsafe_allow_html=True)
        with hc2:
            if st.button("", icon=":material/close:", key="chat_close", help="Close"):
                st.session_state["chat_open"] = False
                st.rerun()

        log = st.session_state["widget_chat_log"]
        if not log:
            st.markdown('<div class="rp-chat-greet">Ask me anything about this run — what matched, '
                       'why, or how the agent works.</div>', unsafe_allow_html=True)
        else:
            for turn in log[-6:]:
                st.markdown(f'<div class="rp-chat-q">{turn["q"]}</div>'
                           f'<div class="rp-chat-msg">{turn["answer"]}'
                           f'<div class="rp-chat-source">{turn["source"]}</div></div>',
                           unsafe_allow_html=True)

        chips = _chips_for(page_context, settlements, focused_txn, active_tab)
        chip_clicked = None
        chip_cols = st.columns(len(chips))
        for i, (col, q) in enumerate(zip(chip_cols, chips)):
            with col:
                st.markdown(f'<div class="st-key-chatchip_{i}">', unsafe_allow_html=True)
                if st.button(q if len(q) < 26 else q[:24] + "…", key=f"chatchip_{i}",
                            width="stretch", help=q):
                    chip_clicked = q
                st.markdown('</div>', unsafe_allow_html=True)

        with st.form(key="chat_form", clear_on_submit=True, border=False):
            typed = st.text_input("Ask a question", label_visibility="collapsed",
                                  placeholder="Type a question…")
            sent = st.form_submit_button("Send", width="stretch")

        active_q = chip_clicked or (typed if sent and typed else None)
        if active_q:
            result = assistant.ask(active_q, metrics or {}, settlements or [], use_llm=use_llm)
            intent = filter_intent(active_q, settlements or [])
            if intent:
                if "band" in intent:
                    st.session_state["recon_band_filter"] = intent["band"]
                if "layer" in intent:
                    st.session_state["recon_layer_dd"] = intent["layer"]
                    st.session_state["layer_filter_active"] = intent["layer"]
                if "search" in intent:
                    st.session_state["recon_search"] = intent["search"]
                    st.session_state.setdefault("expanded_rows", set()).add(intent["search"])
                st.session_state["active_tab"] = "Reconciliation"
                result = {**result, "answer": result["answer"]
                         + " I've applied that filter on the Reconciliation tab."}
            st.session_state["widget_chat_log"].append({"q": active_q, **result})
            st.session_state["chat_open"] = True
            st.rerun()

        st.markdown('<div class="rp-chat-caption">Powered by the same agent, read-only — '
                   'it can\'t change any data.</div>', unsafe_allow_html=True)

    with st.container(key="chat_fab"):
        fab_icon = ":material/close:" if st.session_state["chat_open"] else ":material/chat_bubble:"
        if st.button("", icon=fab_icon, key="chat_fab_btn", help="Ask about this reconciliation"):
            st.session_state["chat_open"] = not st.session_state["chat_open"]
            st.rerun()
