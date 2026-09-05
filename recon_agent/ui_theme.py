"""Razorpay brand system — exact hex per spec, no other colors anywhere.

Design register: confident, minimal, blue-and-white, industrial fintech —
the same visual language as razorpay.com and the Razorpay Dashboard, not a
colorful SaaS product. Line icons only (Feather/Lucide-style, 1.5px stroke,
no fill); the emoji budget for the whole app is exactly two characters
(✓ for reconciled/waterfall lines, — for empty states) and both are used
only where this module's helpers place them — never decoratively in app.py.

Button variants that Streamlit's native `type="primary"/"secondary"/"tertiary"`
can't express (outline-red Reject, outline-blue Preview/dry-run, ghost icon
toggle) are styled via Streamlit's documented `key="..."` -> `.st-key-<key>`
CSS class convention — not a DOM hack, an intentional public API.
"""
from __future__ import annotations

# ---- brand tokens, exact hex, nothing else -------------------------------
PRUSSIAN_BLUE = "#012652"   # nav bar, primary headings, dark-mode base
DODGER_BLUE = "#0D94FB"     # the ONLY accent
WHITE = "#FFFFFF"
OFF_WHITE = "#F5F7FA"
NEAR_BLACK = "#1A1F2B"      # body text
SLATE = "#6B7280"           # secondary text, placeholders, disabled
LIGHT_GRAY = "#E5E8EC"      # borders/dividers, 1px only
GREEN = "#16A34A"           # reconciled/success state only
AMBER = "#D97706"           # needs-review state only
RED = "#DC2626"             # confirmed failure/exception state only

STATUS_COLOR = {"matched": GREEN, "pending_confirmation": AMBER, "exception": RED}
STATUS_LABEL = {"matched": "Reconciled", "pending_confirmation": "Needs review", "exception": "Exception"}
BAND_COLOR = {"High": GREEN, "Needs review": AMBER, "Uncertain": RED}


def band_for(confidence) -> str:
    if confidence is None:
        return "Uncertain"
    if confidence >= 80:
        return "High"
    if confidence >= 55:
        return "Needs review"
    return "Uncertain"


# ---- line icons (Feather-style, 1.5px stroke, currentColor) --------------
_ICON_PATHS = {
    "search": '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "filter": '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
    "settings": ('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 '
                '2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 '
                '9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 '
                '1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 '
                '0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 '
                '1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0 '
                '-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
    "chevron-left": '<polyline points="15 18 9 12 15 6"/>',
    "chevron-right": '<polyline points="9 18 15 12 9 6"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "sun": ('<circle cx="12" cy="12" r="4"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" '
           'y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" '
           'y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line '
           'x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>'),
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "external-link": ('<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>'
                      '<polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>'),
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "link": ('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>'
            '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'),
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "check-circle": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    "help-circle": ('<circle cx="12" cy="12" r="10"/>'
                    '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),
    "minus-circle": '<circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/>',
    "arrow-up": '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>',
    "arrow-down": '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>',
    "minus": '<line x1="5" y1="12" x2="19" y2="12"/>',
    "flag": '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>',
    "corner-down-left": '<polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/>',
}


def icon(name: str, size: int = 15, color: str = "currentColor") -> str:
    body = _ICON_PATHS.get(name, "")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
           f'fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" '
           f'stroke-linejoin="round" style="vertical-align:-3px">{body}</svg>')


CSS = f"""
<style>
:root {{
    --rp-blue: {DODGER_BLUE}; --rp-navy: {PRUSSIAN_BLUE}; --rp-ink: {NEAR_BLACK};
    --rp-slate: {SLATE}; --rp-border: {LIGHT_GRAY}; --rp-offwhite: {OFF_WHITE};
    --rp-green: {GREEN}; --rp-amber: {AMBER}; --rp-red: {RED};
}}

html, body, [class*="css"] {{ font-variant-numeric: lining-nums tabular-nums; }}

.rp-amount, .rp-mono {{
    font-variant-numeric: lining-nums tabular-nums;
    font-feature-settings: "tnum" 1, "lnum" 1;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-weight: 500;
}}

h1, h2, h3 {{ letter-spacing: -0.02em; font-weight: 700; color: {PRUSSIAN_BLUE}; }}
[data-testid="stMetricValue"] {{
    font-variant-numeric: lining-nums tabular-nums;
    color: {DODGER_BLUE} !important; font-weight: 700 !important;
}}
[data-testid="stMetricLabel"] {{ color: {SLATE} !important; font-size: 0.82rem !important; }}
[data-testid="stCaptionContainer"] {{ color: {SLATE} !important; }}

/* ---- status dot (6px circle) — never a colored background chip ---- */
.rp-dot {{ display:inline-block; width:6px; height:6px; border-radius:50%; margin-right:6px; }}
.rp-dot-green {{ background: {GREEN}; }}
.rp-dot-amber {{ background: {AMBER}; }}
.rp-dot-red {{ background: {RED}; }}
.rp-status-row {{ display:inline-flex; align-items:center; font-size: 13px; color: {NEAR_BLACK}; }}

/* ---- outline label chip for layer/source/rule provenance — neutral ---- */
.rp-tag {{
    display:inline-block; padding: 1px 8px; border-radius: 4px;
    border: 1px solid {LIGHT_GRAY}; color: {SLATE}; font-size: 11.5px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
}}

/* ---- plain card, no shadow, no color fill ---- */
.rp-card {{ border: 1px solid {LIGHT_GRAY}; border-radius: 6px; padding: 14px 16px; background: {WHITE}; }}
.rp-card + .rp-card {{ margin-top: 8px; }}
.rp-panel {{ box-shadow: -2px 0 8px rgba(1,38,82,0.06); }}

.rp-divider {{ border: none; border-top: 1px solid {LIGHT_GRAY}; margin: 8px 0; }}

.rp-empty {{ color: {SLATE}; font-size: 13px; }}

/* ---- top nav bar (Prussian Blue) ---- */
.st-key-topnav {{ background: {PRUSSIAN_BLUE}; padding: 14px 20px; border-radius: 0; margin: -1rem -1rem 1rem -1rem; }}
.st-key-topnav p, .st-key-topnav span, .st-key-topnav div {{ color: {WHITE} !important; }}
.st-key-topnav [data-testid="stCaptionContainer"] {{ color: #A9C4E0 !important; }}

/* ---- bottom bulk bar (Prussian Blue) ---- */
.st-key-bulkbar {{ background: {PRUSSIAN_BLUE}; padding: 12px 18px; border-radius: 8px; }}
.st-key-bulkbar p, .st-key-bulkbar span, .st-key-bulkbar div {{ color: {WHITE} !important; }}
.st-key-bulkbar button {{ background: {WHITE} !important; color: {PRUSSIAN_BLUE} !important; border: none !important; }}

/* ---- Reject button: outline, red text, gray border, red border on hover ---- */
[class*="st-key-reject_"] button {{
    background: transparent !important; color: {RED} !important;
    border: 1px solid {LIGHT_GRAY} !important; box-shadow: none !important;
}}
[class*="st-key-reject_"] button:hover {{ border-color: {RED} !important; }}

/* ---- Edit button: outline, navy text ---- */
[class*="st-key-edit_"] button {{
    background: transparent !important; color: {PRUSSIAN_BLUE} !important;
    border: 1px solid {LIGHT_GRAY} !important; box-shadow: none !important;
}}

/* ---- Preview / dry-run button: outline, blue text+border always ---- */
[class*="st-key-dryrun"] button, [class*="st-key-preview_"] button {{
    background: transparent !important; color: {DODGER_BLUE} !important;
    border: 1px solid {DODGER_BLUE} !important; box-shadow: none !important;
}}

/* ---- destructive settings action: white bg, red border+text, never solid ---- */
[class*="st-key-destructive_"] button {{
    background: {WHITE} !important; color: {RED} !important;
    border: 1px solid {RED} !important; box-shadow: none !important;
}}

/* ---- ghost icon button (theme toggle) ---- */
.st-key-theme_toggle button {{
    background: transparent !important; border: none !important; color: {SLATE} !important;
    box-shadow: none !important; padding: 4px 8px !important;
}}

/* ---- tabs: underline style only, no pill background (matches site nav) ---- */
[data-testid="stTabs"] [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {LIGHT_GRAY}; }}
[data-testid="stTabs"] button[role="tab"] {{
    background: transparent !important; color: {SLATE} !important;
    border: none !important; border-bottom: 2px solid transparent !important;
    font-weight: 500 !important;
}}
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {{
    color: {PRUSSIAN_BLUE} !important; border-bottom: 2px solid {DODGER_BLUE} !important;
}}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {{ background: transparent !important; }}

/* ---- inputs ---- */
input, textarea, select {{ border-color: {LIGHT_GRAY} !important; }}
input::placeholder, textarea::placeholder {{ color: {SLATE} !important; }}

/* ---- dataframes: no zebra striping, thin dividers only ---- */
[data-testid="stDataFrame"] {{ border: 1px solid {LIGHT_GRAY}; border-radius: 6px; }}

/* ---- confidence pill: icon + word + small secondary number, colored bg ---- */
.rp-conf-pill {{
    display:inline-flex; align-items:center; gap:5px; padding:3px 10px 3px 8px;
    border-radius:999px; font-size:12.5px; font-weight:600;
}}
.rp-conf-pill .n {{ font-size:10.5px; font-weight:500; opacity:0.75; margin-left:2px; }}
.rp-conf-high {{ background:{GREEN}1A; color:{GREEN}; }}
.rp-conf-medium {{ background:{AMBER}1A; color:{AMBER}; }}
.rp-conf-low {{ background:{SLATE}1A; color:{SLATE}; }}

/* ---- autonomy badge: outline, neutral ---- */
.rp-autonomy-badge {{
    display:inline-flex; align-items:center; gap:5px; padding:2px 9px; border-radius:5px;
    border:1px dashed {LIGHT_GRAY}; color:{SLATE}; font-size:11px; font-weight:500;
}}

/* ---- because-sentence rationale line ---- */
.rp-because {{ font-size:13px; color:{NEAR_BLACK}; line-height:1.5; }}
.rp-because b {{ color:{PRUSSIAN_BLUE}; }}

/* ---- KPI tile trend arrow ---- */
.rp-trend {{ display:inline-flex; align-items:center; gap:2px; font-size:12px; font-weight:600; }}
.rp-trend-up {{ color:{SLATE}; }}
.rp-trend-down {{ color:{SLATE}; }}
.rp-trend-flat {{ color:{SLATE}; }}

/* ---- intent-preview mini pipeline (pre-run state) ---- */
.rp-mini-pipe {{ display:flex; align-items:flex-start; position:relative; margin:20px 0 4px; }}
.rp-mini-pipe::before {{
    content:""; position:absolute; top:14px; left:6%; right:6%; height:1px; background:{LIGHT_GRAY}; z-index:0;
}}
.rp-mini-step {{ position:relative; z-index:1; flex:1; text-align:center; padding:0 4px; }}
.rp-mini-node {{
    width:28px; height:28px; border-radius:50%; background:{WHITE}; border:1.5px solid {LIGHT_GRAY};
    color:{SLATE}; display:flex; align-items:center; justify-content:center; font-size:12px;
    font-weight:700; margin:0 auto 8px;
}}
.rp-mini-step .lbl {{ font-size:11.5px; color:{NEAR_BLACK}; font-weight:500; line-height:1.3; }}

/* ---- clickable layer bar segments ---- */
.rp-layerbar {{ display:flex; height:10px; border-radius:5px; overflow:hidden; background:{LIGHT_GRAY}; }}
.rp-layerbar-seg {{ height:100%; transition:opacity 150ms ease-out; }}
.rp-layerbar-seg:hover {{ opacity:0.8; }}

/* ---- activity drawer ---- */
.rp-drawer-row {{
    display:grid; grid-template-columns: 150px 1fr 140px 110px; gap:10px; font-size:12px;
    padding:5px 0; border-bottom:1px solid {LIGHT_GRAY}; color:{NEAR_BLACK};
}}
.rp-drawer-row.head {{ color:{SLATE}; font-weight:600; text-transform:uppercase; letter-spacing:0.03em; font-size:10.5px; }}

/* ---- guided tour banner: an ordinary in-flow block at the top of the
   dashboard, not fixed-position -- a fixed box at bottom-left sat directly
   on top of the sidebar's own left-hand strip (same 0-336px region a
   collapsed/expanded Streamlit sidebar occupies), which visually stacked
   it over sidebar controls and made its buttons unreliable to click. An
   in-flow banner can never collide with anything else on the page. */
.st-key-tour_overlay {{
    background: {PRUSSIAN_BLUE}; border-radius: 10px; padding: 14px 18px;
    margin-bottom: 14px; animation: rp-kpi-pop 220ms ease-out;
}}
.rp-tour-title {{ display:flex; align-items:center; gap:7px; font-weight:700; color:{WHITE}; font-size:13.5px; margin-bottom:4px; }}
.rp-tour-title svg {{ stroke:{DODGER_BLUE}; }}
.rp-tour-text {{ color:#C7D3E0; font-size:12.5px; line-height:1.5; margin-bottom:10px; }}
.st-key-tour_overlay button {{ font-size:12px !important; }}
</style>
"""


def confidence_pill(confidence: int | None) -> str:
    """Colored pill, icon + word primary, raw number small/secondary — never
    a bare percentage as the main read."""
    if confidence is None:
        confidence = 0
    if confidence >= 80:
        cls, ic, word = "rp-conf-high", "check-circle", "High confidence"
    elif confidence >= 55:
        cls, ic, word = "rp-conf-medium", "help-circle", "Medium confidence"
    else:
        cls, ic, word = "rp-conf-low", "minus-circle", "Low confidence"
    return (f'<span class="rp-conf-pill {cls}">{icon(ic, 13)}{word}'
           f'<span class="n">{confidence}</span></span>')


def autonomy_badge(label: str) -> str:
    return f'<span class="rp-autonomy-badge">{icon("flag", 11)}{label}</span>'


def trend_arrow(delta: float | None, *, pct_points: bool = True) -> str:
    """Up/down/neutral arrow for a KPI tile, never color alone (icon shape
    differs per direction, not just color)."""
    if delta is None:
        return ""
    if abs(delta) < 0.05:
        return f'<span class="rp-trend rp-trend-flat">{icon("minus", 11)}flat</span>'
    unit = "pp" if pct_points else ""
    if delta > 0:
        return f'<span class="rp-trend rp-trend-up">{icon("arrow-up", 11)}{delta:+.1f}{unit}</span>'
    return f'<span class="rp-trend rp-trend-down">{icon("arrow-down", 11)}{delta:+.1f}{unit}</span>'


def sparkline_svg(values: list[float], *, width: int = 64, height: int = 20,
                  color: str = DODGER_BLUE) -> str:
    """Tiny inline trend line across recent runs for a KPI tile — plain
    SVG (no JS/iframe needed, unlike the count-up primitives in motion.py),
    real history values only, never a placeholder shape. Fewer than 2
    points can't describe a trend, so callers get an empty string back."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pad = 2
    pts = [
        (pad + i * (width - 2 * pad) / (n - 1),
         height - pad - (v - lo) / span * (height - 2 * pad))
        for i, v in enumerate(values)
    ]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    last_x, last_y = pts[-1]
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'style="display:block" class="rp-spark">'
           f'<polyline points="{path}" pathLength="100" fill="none" stroke="{color}" stroke-width="1.5" '
           f'stroke-linejoin="round" stroke-linecap="round"/>'
           f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2" fill="{color}"/>'
           f'</svg>')


_LAYER_VERBS = {
    "exact_reference+deduction_engine": "closed it automatically",
    "exact_reference+partial_payment": "recorded it as a partial payment",
    "exact_reference_batch+deduction_engine": "closed the batch automatically",
    "anchored_batch_completion": "completed the batch from the confirmed anchor",
    "amount_date_subset_sum": "closed it via a unique amount/date match",
    "llm_investigator": "proposed a match for human confirmation",
}


def because_sentence(s: dict, invoices: list[dict]) -> str:
    """'**Because** [evidence], agent [action].' — built from the settlement's
    own structured fields (layer, deduction_label, matched invoice reference),
    not a regex over the free-text rationale, so it can't drift out of sync
    with what the layer actually did."""
    layer = s["layer"]
    matched = [i for i in invoices if i["invoice_id"] in s["matched_invoice_ids"]]
    label = s.get("deduction_label")

    if layer in ("exact_reference+deduction_engine", "exact_reference+partial_payment") and matched:
        ref = matched[0]["reference_code"]
        clause = f"reference '{ref}' was found in the narration"
        if label and label != "none":
            clause += f" and {label} explains the amount"
    elif layer == "exact_reference_batch+deduction_engine" and matched:
        clause = f"{len(matched)} reference codes were found in one narration"
        if label and label != "none":
            clause += f" and {label} explains the combined amount"
    elif layer == "anchored_batch_completion":
        clause = "one or more reference codes were confirmed and the remaining narration text matched the rest"
    elif layer == "amount_date_subset_sum":
        clause = "no usable reference existed, but a unique amount and date combination matched"
    elif layer == "llm_investigator" and s["status"] == "pending_confirmation":
        clause = f"the Tier-1 investigator scored this at {s['confidence']}/100 confidence"
    elif layer == "llm_investigator":
        clause = "no candidate cleared the confidence bar for a confident proposal"
    else:
        clause = (s.get("rationale") or "the evidence below").rstrip(".")

    verb = _LAYER_VERBS.get(layer, "recorded a decision")
    return f'<span class="rp-because"><b>Because</b> {clause}, agent {verb}.</span>'


def mini_pipeline(step5_label: str) -> str:
    """The 5-step intent-preview pipeline shown before a run — step 5's
    label is passed in by the caller, since it depends on the live
    'Autonomy per rule' sidebar setting, not a fixed string."""
    steps = [
        ("1", "Exact match"), ("2", "Deduction formulas"), ("3", "Batch / subset-sum"),
        ("4", "Partial payments"), ("5", step5_label),
    ]
    cells = "".join(
        f'<div class="rp-mini-step"><div class="rp-mini-node">{n}</div><div class="lbl">{lbl}</div></div>'
        for n, lbl in steps
    )
    return f'<div class="rp-mini-pipe">{cells}</div>'


def status_row(status: str) -> str:
    """Colored dot + neutral-ink text — never a filled colored pill."""
    dot_cls = {"matched": "rp-dot-green", "pending_confirmation": "rp-dot-amber",
              "exception": "rp-dot-red"}.get(status, "rp-dot-amber")
    label = STATUS_LABEL.get(status, status)
    return f'<span class="rp-status-row"><span class="rp-dot {dot_cls}"></span>{label}</span>'


def band_row(confidence) -> str:
    band = band_for(confidence)
    dot_cls = {"High": "rp-dot-green", "Needs review": "rp-dot-amber",
              "Uncertain": "rp-dot-red"}[band]
    return f'<span class="rp-status-row"><span class="rp-dot {dot_cls}"></span>{band}</span>'


def tag(text: str | None) -> str:
    if not text:
        return "—"
    return f'<span class="rp-tag">{text}</span>'


def fmt_inr_compact(amount) -> str:
    """Short form for KPI tiles, in the lakh/crore convention an Indian
    finance team actually reads: 4523.1 -> '₹4,523', 152000 -> '₹1.52L',
    31500000 -> '₹3.15Cr'. Full precision still belongs in tables and the
    row-level math panel — this is for headline tiles only, where the exact
    paise matter less than the magnitude landing instantly."""
    if amount is None:
        return "—"
    sign = "-" if amount < 0 else ""
    a = abs(float(amount))
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:.2f}Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:.2f}L"
    return f"{sign}{fmt_inr(round(a))[:-3] if fmt_inr(round(a)).endswith('.00') else fmt_inr(round(a))}"


def fmt_inr(amount) -> str:
    """Indian digit grouping, tabular-nums applied via .rp-amount at render
    sites. 12345678.9 -> '₹1,23,45,678.90'"""
    if amount is None:
        return "—"
    neg = amount < 0
    amount = abs(float(amount))
    whole = int(amount)
    frac = round((amount - whole) * 100)
    if frac == 100:
        whole += 1
        frac = 0
    s = str(whole)
    if len(s) <= 3:
        grouped = s
    else:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join(parts) + "," + tail
    body = f"₹{grouped}.{frac:02d}"
    return f"−{body}" if neg else body
