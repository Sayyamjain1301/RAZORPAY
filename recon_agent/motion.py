"""Animation and micro-interaction primitives.

Every animation here exists to communicate a state change, never decoration.
Two implementation strategies, chosen per-case by what Streamlit's rerun
model actually allows:

  1. Pure CSS transitions/keyframes (button hover, badge fade-in, row pulse,
     panel slide, tab underline) — these fire correctly across a Streamlit
     rerun as long as the DOM node's identity is stable (same structural
     position / same `key`), which is true everywhere they're used here.
  2. Self-contained `components.v1.html` widgets for anything that needs a
     JS-driven numeric tween or a staged reveal with real timing (count-up,
     the pipeline ticker, the bulk-bar countdown) — each is a standalone
     iframe that only displays a value, never talks back to Python, so the
     iframe sandbox that blocks two-way communication is a non-issue here.

`prefers-reduced-motion` is honored in both: the global CSS media query
below forces every transition/animation to ~0 for pure-CSS effects, and
every JS snippet checks `matchMedia` itself and jumps straight to the final
state when the user has asked for reduced motion.

Where a sleep()+rerun pairing appears in app.py, it exists to hold the
current (already-rendered) frame on screen long enough for its CSS/JS
animation to finish playing before the next state change arrives — the
underlying data changes we're pacing are already real by that point, this
is UI pacing, not a fake loading bar.
"""
from __future__ import annotations

import streamlit as st

# ---------------------------------------------------------------- durations
MICRO_MS = 120          # hover/focus/checkbox
PANEL_MS = 220          # panel slide, row expand
TAB_MS = 180            # tab underline slide
PULSE_IN_MS = 150
PULSE_HOLD_MS = 200
PULSE_OUT_MS = 150
PULSE_TOTAL_MS = PULSE_IN_MS + PULSE_HOLD_MS + PULSE_OUT_MS   # 500, spec-defined composite
COLLAPSE_MS = 200
CROSSFADE_MS = 150
BADGE_FADE_MS = 150
COUNTUP_MS = 800
STAGGER_MS = 15
RISE_MS = 320             # staggered card/row entry
RISE_STAGGER_MS = 55      # gap between consecutive items in one group
TICKER_STAGGER_MS = 300
TICKER_CAP_MS = 2500
PIPELINE_FLOW_STAGGER_MS = 450
PIPELINE_FLOW_COUNT_MS = 550
PIPELINE_FLOW_N_NODES = 5
# 4 gaps between 5 nodes, plus the last node's own count-up, plus a small buffer
PIPELINE_FLOW_TOTAL_MS = (PIPELINE_FLOW_N_NODES - 1) * PIPELINE_FLOW_STAGGER_MS + PIPELINE_FLOW_COUNT_MS + 200

CSS = f"""
<style>
@media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{
        animation-duration: 0.001ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.001ms !important;
        scroll-behavior: auto !important;
    }}
}}

/* ---- buttons: 100ms ease-out hover darken, immediate press dip (#9) ---- */
button {{ transition: background-color {MICRO_MS}ms ease-out, opacity {MICRO_MS}ms ease-out,
                     border-color {MICRO_MS}ms ease-out !important; }}
button:active {{ opacity: 0.85 !important; transition: opacity 0ms !important; }}

/* ---- checkbox / focus micro (100-150ms) ---- */
input[type="checkbox"], input, textarea, select {{
    transition: border-color {MICRO_MS}ms ease-out, box-shadow {MICRO_MS}ms ease-out !important;
}}

/* ---- review panel slide-in from the right (#2) ---- */
@keyframes rp-panel-slide {{
    from {{ transform: translateX(24px); opacity: 0; }}
    to   {{ transform: translateX(0);    opacity: 1; }}
}}
.rp-panel-enter {{ animation: rp-panel-slide {PANEL_MS}ms ease-out; }}

/* ---- source row: 1px Dodger Blue left border while panel is open (#2) --- */
.rp-row-active {{ border-left: 1px solid #0D94FB; transition: border-color {MICRO_MS}ms ease-out; }}
.rp-row-inactive {{ border-left: 1px solid transparent; }}

/* ---- confirm pulse -> collapse, composite 500ms + 200ms (#3, #4) ------- */
@keyframes rp-row-pulse {{
    0%   {{ background-color: transparent; }}
    30%  {{ background-color: rgba(22,163,74,0.10); }}
    70%  {{ background-color: rgba(22,163,74,0.10); }}
    100% {{ background-color: transparent; }}
}}
@keyframes rp-row-collapse {{
    from {{ max-height: 60px; opacity: 1; margin: 0; padding: inherit; }}
    to   {{ max-height: 0;    opacity: 0; margin: 0; padding: 0; overflow: hidden; }}
}}
.rp-confirm-pulse {{
    animation: rp-row-pulse {PULSE_TOTAL_MS}ms ease-out;
}}
.rp-confirm-pulse-collapse {{
    animation: rp-row-pulse {PULSE_TOTAL_MS}ms ease-out,
              rp-row-collapse {COLLAPSE_MS}ms ease-out {PULSE_TOTAL_MS}ms forwards;
}}

/* ---- badge crossfade: "Pending" -> "Matched" (#3) ---------------------- */
@keyframes rp-fade-out {{ from {{ opacity: 1; }} to {{ opacity: 0; }} }}
@keyframes rp-fade-in  {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
.rp-badge-old {{ animation: rp-fade-out {CROSSFADE_MS}ms ease-out forwards; position: absolute; }}
.rp-badge-new {{ animation: rp-fade-in {CROSSFADE_MS}ms ease-out forwards; }}
.rp-badge-wrap {{ position: relative; display: inline-block; }}

/* ---- calm badge fade-in on a newly-seen decision (#8) — never a pulse -- */
@keyframes rp-soft-fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
.rp-badge-fresh {{ animation: rp-soft-fade {BADGE_FADE_MS}ms ease-out; }}

/* ---- skeleton shimmer, loads only, matches real row height (#6) -------- */
@keyframes rp-shimmer {{
    0%   {{ background-position: -300px 0; }}
    100% {{ background-position: 300px 0; }}
}}
.rp-skel-row {{
    height: 34px; border-radius: 4px; margin-bottom: 8px;
    background: linear-gradient(90deg, #F5F7FA 25%, #ECEFF3 37%, #F5F7FA 63%);
    background-size: 600px 100%;
    animation: rp-shimmer 1.5s linear infinite;
}}

/* ---- custom sliding tab underline (#10) -------------------------------- */
.rp-tabbar {{ position: relative; border-bottom: 1px solid #E5E8EC; margin-bottom: 4px; }}
.rp-tabbar-underline {{
    position: absolute; bottom: -1px; height: 2px; background: #0D94FB;
    transition: left {TAB_MS}ms ease-out, width {TAB_MS}ms ease-out;
}}
.rp-tabbtn button {{
    background: transparent !important; border: none !important; box-shadow: none !important;
    color: #6B7280 !important; font-weight: 500 !important; border-radius: 0 !important;
    padding-bottom: 10px !important;
}}
.rp-tabbtn-active button {{ color: #012652 !important; }}

/* ---- KPI tile value: soft pop-in the first time a run's value is shown -- */
@keyframes rp-kpi-pop {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: translateY(0); }} }}
.rp-kpi-fresh {{ animation: rp-kpi-pop 300ms ease-out; display: inline-block; }}

/* ---- staggered reveal: a group of related cards/rows arriving in reading
   order rather than all at once. Delay is set inline per item by the call
   site (Streamlit puts each column in its own DOM subtree, so nth-child
   can't see across them). Applied ONLY via app.py's once-per-run gate, so
   changing a filter or switching tabs never replays it. */
@keyframes rp-rise-in {{
    from {{ opacity: 0; transform: translateY(6px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}
.rp-rise {{ animation: rp-rise-in {RISE_MS}ms ease-out both; }}

/* ---- KPI tile sparkline: draws in left-to-right on first paint --------- */
@keyframes rp-spark-draw {{ from {{ stroke-dashoffset: 100; }} to {{ stroke-dashoffset: 0; }} }}
.rp-spark polyline {{ stroke-dasharray: 100; animation: rp-spark-draw 500ms ease-out 200ms both; }}

/* ---- layer bar segments grow from 0 on first paint, staggered ---------- */
/* No explicit `to` keyframe: the browser fills it in from each segment's
   own inline `style="width:X%"`, so this animates 0 -> its real width
   without JS needing to know the target. */
@keyframes rp-seg-grow {{ from {{ width: 0; }} }}
.rp-layerbar-seg {{ animation: rp-seg-grow 500ms ease-out both; }}
.rp-layerbar-seg:nth-child(1) {{ animation-delay: 0ms; }}
.rp-layerbar-seg:nth-child(2) {{ animation-delay: 60ms; }}
.rp-layerbar-seg:nth-child(3) {{ animation-delay: 120ms; }}
.rp-layerbar-seg:nth-child(4) {{ animation-delay: 180ms; }}
.rp-layerbar-seg:nth-child(5) {{ animation-delay: 240ms; }}
.rp-layerbar-seg:nth-child(6) {{ animation-delay: 300ms; }}

/* ---- activity log drawer: slides up from the bottom of the results pane -- */
.st-key-activity_drawer {{
    overflow: hidden; transition: max-height 200ms ease-out;
}}
</style>
"""

DRAWER_MS = 200


def reduced_motion_guard(js_var: str = "reduced") -> str:
    return f"const {js_var} = window.matchMedia('(prefers-reduced-motion: reduce)').matches;"


def count_up(value: float, *, start: float = 0.0, decimals: int = 1, suffix: str = "%",
            prefix: str = "", duration_ms: int = COUNTUP_MS, font_size: str = "2.3rem",
            color: str = "#0D94FB", weight: int = 700, height: int = 60,
            elem_id: str = "cu") -> None:
    """Renders once; tweens `start` -> `value` on mount, ease-out cubic.

    `start` defaults to 0 (a first-ever reading counts up from nothing), but
    callers that know the PREVIOUS run's figure should pass it: animating
    85.2 -> 87.9 shows the movement between two real states, which a
    0 -> 87.9 count-up structurally cannot — the number visibly travels the
    distance the run actually moved it, and the direction is legible before
    you read the delta caption underneath. Standard data-viz motion
    guidance treats that state-to-state transition as the animation that
    carries real information, versus a mount flourish that carries none.

    Caller is responsible for only invoking this when the value is genuinely
    new (see app.py's per-run_id animated-once tracking) — a component
    re-mounts and re-animates every time Streamlit re-executes this call, so
    gating when to call it is what makes this 'once on load, never on
    re-render'.
    """
    html = f"""
    <div id="{elem_id}" style="font:{weight} {font_size}/1.1 Inter,-apple-system,sans-serif;
         color:{color}; font-variant-numeric: tabular-nums; letter-spacing:-0.02em;">{prefix}{start:.{decimals}f}{suffix}</div>
    <script>
    (function() {{
        const el = document.getElementById("{elem_id}");
        const from = {start};
        const target = {value};
        {reduced_motion_guard()}
        if (reduced) {{ el.textContent = "{prefix}" + target.toFixed({decimals}) + "{suffix}"; return; }}
        const dur = {duration_ms};
        const t0 = performance.now();
        function ease(t) {{ return 1 - Math.pow(1 - t, 3); }}
        function tick(now) {{
            const p = Math.min(1, (now - t0) / dur);
            const v = from + (target - from) * ease(p);
            el.textContent = "{prefix}" + v.toFixed({decimals}) + "{suffix}";
            if (p < 1) requestAnimationFrame(tick);
            else el.textContent = "{prefix}" + target.toFixed({decimals}) + "{suffix}";
        }}
        requestAnimationFrame(tick);
    }})();
    </script>
    """
    st.iframe(src=html, height=height)


def count_up_grid(items: list[dict], *, duration_ms: int = COUNTUP_MS, height: int = 70) -> None:
    """items: [{label, value, decimals}] — simultaneous count-up, one row,
    used for the rule-preview backtest metrics (#7)."""
    cols = "".join(
        f"""<div style="flex:1;text-align:left">
              <div style="font-size:11.5px;color:#6B7280;margin-bottom:2px">{it['label']}</div>
              <div id="cug-{i}" style="font:700 1.5rem/1.1 Inter,sans-serif;color:#012652;
                   font-variant-numeric:tabular-nums">0</div>
            </div>"""
        for i, it in enumerate(items)
    )
    ticks = "\n".join(
        f"""(function() {{
              const el = document.getElementById("cug-{i}");
              const target = {it['value']};
              if (reduced) {{ el.textContent = target.toFixed({it.get('decimals', 0)}); return; }}
              const start = performance.now(), dur = {duration_ms};
              function ease(t) {{ return 1 - Math.pow(1-t, 3); }}
              function tick(now) {{
                  const p = Math.min(1, (now-start)/dur);
                  el.textContent = (target*ease(p)).toFixed({it.get('decimals', 0)});
                  if (p < 1) requestAnimationFrame(tick); else el.textContent = target.toFixed({it.get('decimals', 0)});
              }}
              requestAnimationFrame(tick);
            }})();"""
        for i, it in enumerate(items)
    )
    html = f"""
    <div style="display:flex;gap:24px">{cols}</div>
    <script>
    {reduced_motion_guard()}
    {ticks}
    </script>
    """
    st.iframe(src=html, height=height)


def pipeline_ticker(lines: list[str], *, stagger_ms: int = TICKER_STAGGER_MS,
                    height: int = 150) -> None:
    """Staged reveal of REAL, already-computed per-layer results (#5). The
    numbers are never fabricated — see app.py's call site, which passes the
    actual layer counts from the run that just finished. The staggered
    reveal is a deliberate legibility pace on top of a completed
    computation, not a simulation of one still in progress."""
    items_js = ",".join('"' + l.replace('"', '\\"') + '"' for l in lines)
    html = f"""
    <div id="ticker" style="font:500 13px/1.7 ui-monospace,'SF Mono',Menlo,monospace;color:#1A1F2B"></div>
    <script>
    (function() {{
        const lines = [{items_js}];
        const el = document.getElementById("ticker");
        {reduced_motion_guard()}
        const stagger = reduced ? 0 : {stagger_ms};
        lines.forEach((line, i) => {{
            const div = document.createElement("div");
            div.textContent = line;
            div.style.opacity = 0;
            div.style.transform = reduced ? "none" : "translateY(4px)";
            div.style.transition = "opacity 200ms ease-out, transform 200ms ease-out";
            el.appendChild(div);
            setTimeout(() => {{ div.style.opacity = 1; div.style.transform = "translateY(0)"; }}, i * stagger);
        }});
    }})();
    </script>
    """
    st.iframe(src=html, height=height)


def count_down_to_zero(start_value: int, *, duration_ms: int = 500, height: int = 40) -> None:
    """Bulk bar's selection count animating down to zero before it fades (#4)."""
    html = f"""
    <div id="cd" style="font:600 1rem/1.2 Inter,sans-serif;color:#FFFFFF;
         font-variant-numeric:tabular-nums">{start_value} selected</div>
    <script>
    (function() {{
        const el = document.getElementById("cd");
        const start = {start_value};
        {reduced_motion_guard()}
        if (reduced) {{ el.textContent = "0 selected"; return; }}
        const t0 = performance.now(), dur = {duration_ms};
        function ease(t) {{ return 1 - Math.pow(1-t, 3); }}
        function tick(now) {{
            const p = Math.min(1, (now-t0)/dur);
            const v = Math.round(start * (1 - ease(p)));
            el.textContent = v + " selected";
            if (p < 1) requestAnimationFrame(tick); else el.textContent = "0 selected";
        }}
        requestAnimationFrame(tick);
    }})();
    </script>
    """
    st.iframe(src=html, height=height)


def pipeline_flow(nodes: list[dict], *, stagger_ms: int = 450, count_ms: int = 550,
                  height: int = 150, elem_id: str = "pflow") -> None:
    """The centerpiece pipeline visualization: N nodes left-to-right, each
    counting up from 0 to its REAL settlement count (caller passes actual
    per-layer counts from the run that just finished — see app.py's call
    site — never fabricated), with the connecting line filling in behind
    it and the node lighting up (border + fill) exactly when its count-up
    finishes. Node N (the LLM investigator, by convention the thinnest/last
    slice) lights up last, visually reinforcing that it only ever sees what
    the deterministic layers upstream couldn't close.

    Single self-contained iframe, same reduced-motion guard and easing as
    every other primitive in this module."""
    n = len(nodes)
    node_divs, line_divs, scripts = [], [], []
    for i, item in enumerate(nodes):
        label, count = item["label"], item["count"]
        node_divs.append(f"""
        <div class="pf-node" style="flex:1;text-align:center;position:relative;z-index:1">
            <div id="{elem_id}-n{i}" class="pf-circle" style="
                width:44px;height:44px;border-radius:50%;margin:0 auto 8px;
                background:#fff;border:2px solid #E5E8EC;color:#6B7280;
                display:flex;align-items:center;justify-content:center;
                font:700 15px/1 Inter,sans-serif;font-variant-numeric:tabular-nums;
                transition:border-color 200ms ease-out,background-color 200ms ease-out,color 200ms ease-out;
            ">0</div>
            <div style="font-size:11px;color:#1A1F2B;font-weight:500;line-height:1.3;padding:0 4px">{label}</div>
        </div>""")
        if i < n - 1:
            line_divs.append(f"""
            <div style="flex:0 0 auto;width:{100 // max(n - 1, 1)}%;max-width:60px;
                        height:2px;background:#E5E8EC;position:relative;top:22px;margin-top:-22px;
                        overflow:hidden">
                <div id="{elem_id}-l{i}" style="height:100%;width:0%;background:#0D94FB;
                     transition:width 250ms ease-out"></div>
            </div>""")
        scripts.append(f"""
        (function() {{
            const el = document.getElementById("{elem_id}-n{i}");
            const line = document.getElementById("{elem_id}-l{i}");
            const target = {count};
            const delay = reduced ? 0 : {i * stagger_ms};
            const dur = reduced ? 0 : {count_ms};
            function light() {{
                el.style.borderColor = "#0D94FB";
                el.style.background = "#0D94FB";
                el.style.color = "#fff";
                if (line) line.style.width = "100%";
            }}
            setTimeout(function() {{
                if (dur === 0) {{ el.textContent = target; light(); return; }}
                const start = performance.now();
                function ease(t) {{ return 1 - Math.pow(1 - t, 3); }}
                function tick(now) {{
                    const p = Math.min(1, (now - start) / dur);
                    el.textContent = Math.round(target * ease(p));
                    if (p < 1) requestAnimationFrame(tick);
                    else {{ el.textContent = target; light(); }}
                }}
                requestAnimationFrame(tick);
            }}, delay);
        }})();""")

    row = "".join(v for pair in zip(node_divs, line_divs + [""]) for v in pair if v)
    html = f"""
    <div style="display:flex;align-items:flex-start;padding:8px 4px">{row}</div>
    <script>
    {reduced_motion_guard()}
    {"".join(scripts)}
    </script>
    """
    st.iframe(src=html, height=height)


def skeleton_rows(n: int, *, row_height: int = 34) -> str:
    """Matches the real work-queue row height exactly — used ONLY while a
    real load (data regen / reconciliation run) is in flight, never as
    decoration (#6)."""
    rows = "".join(
        f'<div class="rp-skel-row" style="height:{row_height}px;animation-delay:{i*40}ms"></div>'
        for i in range(n)
    )
    return f'<div>{rows}</div>'
