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
TICKER_STAGGER_MS = 300
TICKER_CAP_MS = 2500

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

/* ---- activity log drawer: slides up from the bottom of the results pane -- */
.st-key-activity_drawer {{
    overflow: hidden; transition: max-height 200ms ease-out;
}}
</style>
"""

DRAWER_MS = 200


def reduced_motion_guard(js_var: str = "reduced") -> str:
    return f"const {js_var} = window.matchMedia('(prefers-reduced-motion: reduce)').matches;"


def count_up(value: float, *, decimals: int = 1, suffix: str = "%", prefix: str = "",
            duration_ms: int = COUNTUP_MS, font_size: str = "2.3rem", color: str = "#0D94FB",
            weight: int = 700, height: int = 60, elem_id: str = "cu") -> None:
    """Renders once; animates 0 -> value on mount, ease-out cubic. Caller is
    responsible for only invoking this when the value is genuinely new (see
    app.py's per-run_id animated-once tracking) — a component re-mounts and
    re-animates every time Streamlit re-executes this call, so gating When to
    call it is what makes this 'once on load, never on re-render'."""
    html = f"""
    <div id="{elem_id}" style="font:{weight} {font_size}/1.1 Inter,-apple-system,sans-serif;
         color:{color}; font-variant-numeric: tabular-nums; letter-spacing:-0.02em;">{prefix}0{suffix}</div>
    <script>
    (function() {{
        const el = document.getElementById("{elem_id}");
        const target = {value};
        {reduced_motion_guard()}
        if (reduced) {{ el.textContent = "{prefix}" + target.toFixed({decimals}) + "{suffix}"; return; }}
        const dur = {duration_ms};
        const start = performance.now();
        function ease(t) {{ return 1 - Math.pow(1 - t, 3); }}
        function tick(now) {{
            const p = Math.min(1, (now - start) / dur);
            const v = target * ease(p);
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


def skeleton_rows(n: int, *, row_height: int = 34) -> str:
    """Matches the real work-queue row height exactly — used ONLY while a
    real load (data regen / reconciliation run) is in flight, never as
    decoration (#6)."""
    rows = "".join(
        f'<div class="rp-skel-row" style="height:{row_height}px;animation-delay:{i*40}ms"></div>'
        for i in range(n)
    )
    return f'<div>{rows}</div>'
