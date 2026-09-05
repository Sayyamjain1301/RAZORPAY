"""Tests for the motion primitives' generated markup.

These animations are shipped as generated HTML/JS strings, so the thing
worth asserting is that the numbers baked into that markup are the real
ones the caller asked for — particularly count_up's `start`, which is what
makes a second run animate from the previous reading instead of from zero.
"""
import re
import sys
import types as _pytypes

import pytest


@pytest.fixture
def captured(monkeypatch):
    """Captures the html string motion.* hands to st.iframe, without
    needing a running Streamlit script context."""
    import recon_agent.motion as motion

    box = {}

    def fake_iframe(src, **kwargs):
        box["html"] = src
        box["kwargs"] = kwargs

    monkeypatch.setattr(motion.st, "iframe", fake_iframe)
    return box


def test_count_up_defaults_to_starting_at_zero(captured):
    from recon_agent import motion
    motion.count_up(87.9)
    html = captured["html"]

    assert "const from = 0.0;" in html
    assert "const target = 87.9;" in html
    # the pre-animation text shown before JS runs is the start value
    assert ">0.0%</div>" in html


def test_count_up_tweens_from_the_previous_reading_when_given_one(captured):
    """A second run should travel 85.2 -> 87.9, not 0 -> 87.9 — the whole
    point of the state-to-state transition."""
    from recon_agent import motion
    motion.count_up(87.9, start=85.2)
    html = captured["html"]

    assert "const from = 85.2;" in html
    assert "const target = 87.9;" in html
    assert ">85.2%</div>" in html


def test_count_up_interpolates_between_from_and_target(captured):
    """The tween must be from + (target-from)*eased, not target*eased —
    the latter would jump to zero on the first frame of a 85 -> 87 move."""
    from recon_agent import motion
    motion.count_up(90.0, start=80.0)

    assert "from + (target - from) * ease(p)" in captured["html"]


def test_count_up_honors_reduced_motion_by_jumping_to_the_target(captured):
    from recon_agent import motion
    motion.count_up(50.0, start=20.0)
    html = captured["html"]

    assert "prefers-reduced-motion" in html
    # under reduced motion it sets the final value and returns, never tweening
    assert re.search(r"if \(reduced\).*target\.toFixed", html, re.S)


def test_count_up_can_count_down(captured):
    """A run that got worse must animate downward, not upward."""
    from recon_agent import motion
    motion.count_up(72.5, start=88.0)
    html = captured["html"]

    assert "const from = 88.0;" in html
    assert "const target = 72.5;" in html


def test_pipeline_flow_bakes_in_the_real_per_node_counts(captured):
    from recon_agent import motion
    motion.pipeline_flow([
        {"label": "Exact match", "count": 31},
        {"label": "Unresolved", "count": 4},
    ])
    html = captured["html"]

    assert "const target = 31;" in html
    assert "const target = 4;" in html
    assert "Exact match" in html and "Unresolved" in html


def test_pipeline_flow_total_duration_covers_every_node(captured):
    """app.py sleeps for PIPELINE_FLOW_TOTAL_MS so the reveal isn't cut off
    mid-animation — that constant must actually cover the last node."""
    from recon_agent import motion

    last_node_starts = (motion.PIPELINE_FLOW_N_NODES - 1) * motion.PIPELINE_FLOW_STAGGER_MS
    last_node_ends = last_node_starts + motion.PIPELINE_FLOW_COUNT_MS
    assert motion.PIPELINE_FLOW_TOTAL_MS >= last_node_ends


def test_rise_in_keyframes_and_class_are_defined(captured):
    from recon_agent import motion

    assert "@keyframes rp-rise-in" in motion.CSS
    assert ".rp-rise" in motion.CSS
    assert f"{motion.RISE_MS}ms" in motion.CSS


def test_reduced_motion_media_query_covers_every_css_animation():
    """Every CSS animation in this module must be disarmed for users who
    ask for reduced motion — the blanket media query is what guarantees it."""
    from recon_agent import motion

    assert "@media (prefers-reduced-motion: reduce)" in motion.CSS
    assert "animation-duration: 0.001ms !important" in motion.CSS
    assert "transition-duration: 0.001ms !important" in motion.CSS
