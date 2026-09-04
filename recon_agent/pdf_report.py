"""One-click, single-page PDF scorecard export (item 8).

Uses reportlab exclusively -- pure Python, no system-level dependency (unlike
weasyprint, which needs Cairo/Pango installed). The tradeoff curve is drawn
as a native reportlab vector chart, not a rasterized image, so no extra
plotting library (matplotlib etc.) is pulled in just for one chart.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PRUSSIAN_BLUE = colors.HexColor("#012652")
DODGER_BLUE = colors.HexColor("#0D94FB")
SLATE = colors.HexColor("#6B7280")
LIGHT_GRAY = colors.HexColor("#E5E8EC")

ARCHITECTURE_SUMMARY = (
    "This agent closes the invoice-to-settlement reconciliation loop with a hybrid "
    "deterministic-plus-LLM pipeline. Layers 1-4 (exact reference matching, a gateway-fee/GST/TDS "
    "deduction-formula engine, anchored batch and subset-sum reconciliation, and partial-payment "
    "tracking) are fully deterministic and auto-close the ledger with zero LLM cost -- every one of "
    "those matches is provable with a calculator. Only what survives all four layers reaches Layer "
    "5, a bounded, read-only Tier-1 Investigator: it proposes a match with a plain-language "
    "rationale and a confidence score, but never writes to the ledger -- every proposal is "
    "surfaced as pending confirmation and requires an explicit human click before it can close an "
    "invoice. If no API key is configured, or a live call fails, the pipeline degrades to a "
    "deterministic rule-based fallback rather than blocking or crashing."
)


def _metric_row(label: str, m: dict | None, pct: bool = True) -> list:
    if not m or m.get("mean") is None:
        return [label, "n/a", "n/a"]
    if pct:
        return [label, f"{m['mean']*100:.1f}%", f"± {m['std']*100:.1f}pp"]
    return [label, f"{m['mean']:.1f}", f"± {m['std']:.1f}"]


def _tradeoff_chart(curve: list[dict]) -> Drawing:
    """Native reportlab vector line chart -- precision and coverage vs.
    confidence threshold, no rasterization / no matplotlib dependency."""
    drawing = Drawing(440, 200)
    chart = HorizontalLineChart()
    chart.x, chart.y = 50, 30
    chart.width, chart.height = 370, 150
    taus = [c["tau"] for c in curve]
    chart.data = [[c["precision"] * 100 for c in curve], [c["coverage"] * 100 for c in curve]]
    chart.categoryAxis.categoryNames = [str(t) if t % 20 == 0 else "" for t in taus]
    chart.categoryAxis.labels.fontSize = 6
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = 100
    chart.valueAxis.valueStep = 20
    chart.lines[0].strokeColor = colors.HexColor("#16A34A")
    chart.lines[0].strokeWidth = 1.5
    chart.lines[1].strokeColor = DODGER_BLUE
    chart.lines[1].strokeWidth = 1.5
    drawing.add(chart)
    return drawing


def build_scorecard_pdf(summary: dict) -> bytes:
    """`summary` is evaluate.py's multi_seed()-shaped dict (must have at
    least 'seeds', 'n_per_seed', and the mean/std metric sub-dicts). Optional
    keys used if present: 'demo_batch' (for the tradeoff curve + honeypots),
    'decision_determinism'."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch,
                            bottomMargin=0.6 * inch, leftMargin=0.7 * inch, rightMargin=0.7 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("RPTitle", parent=styles["Heading1"], textColor=PRUSSIAN_BLUE,
                                 fontSize=18, spaceAfter=2)
    sub_style = ParagraphStyle("RPSub", parent=styles["Normal"], textColor=SLATE, fontSize=9,
                               spaceAfter=14)
    h2_style = ParagraphStyle("RPH2", parent=styles["Heading2"], textColor=PRUSSIAN_BLUE,
                              fontSize=12, spaceBefore=12, spaceAfter=6)
    body_style = ParagraphStyle("RPBody", parent=styles["Normal"], fontSize=9, leading=13)
    caption_style = ParagraphStyle("RPCaption", parent=styles["Normal"], fontSize=7.5,
                                   textColor=SLATE, spaceBefore=4)

    story = [
        Paragraph("Reconciliation Agent — Scorecard", title_style),
        Paragraph("AI Finance Controller · Razorpay AI Buildathon 2026", sub_style),
    ]

    story.append(Paragraph("Architecture", h2_style))
    story.append(Paragraph(ARCHITECTURE_SUMMARY, body_style))

    story.append(Paragraph("Multi-seed scorecard — mean ± std, not a single run", h2_style))
    story.append(Paragraph(
        f"n = {summary.get('n_per_seed', 'n/a')} per seed, {len(summary.get('seeds', []))} seeds "
        f"({', '.join(str(s) for s in summary.get('seeds', []))})", caption_style))

    rows = [["Metric", "Mean", "Std"]]
    rows.append(_metric_row("Auto-match rate", summary.get("auto_match_rate")))
    rows.append(_metric_row("Precision", summary.get("precision")))
    rows.append(_metric_row("Recall", summary.get("recall")))
    rows.append(_metric_row("Deduction-hypothesis accuracy", summary.get("deduction_hypothesis_accuracy")))
    rows.append(_metric_row("Records/sec", summary.get("records_per_sec"), pct=False))
    hp = summary.get("honeypots")
    if hp:
        rows.append(["Honeypots baited", f"{hp['baited']} of {hp['total']}", ""])
    dd = summary.get("decision_determinism")
    if dd:
        rows.append(["Decision determinism (DecDet)", f"{dd.get('value', 0)*100:.1f}%",
                    f"({dd.get('reps', '?')} reruns)"])

    table = Table(rows, colWidths=[2.6 * inch, 1.6 * inch, 1.6 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRUSSIAN_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, LIGHT_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)

    curve = summary.get("threshold_curve")
    if curve and any(c.get("n", 0) > 0 for c in curve):
        story.append(Paragraph("Precision (green) vs. auto-approval coverage (blue) by threshold", h2_style))
        story.append(_tradeoff_chart(curve))
        story.append(Paragraph("X-axis: confidence threshold 0-100. Y-axis: percentage.", caption_style))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"reproducible via evaluate.py, not hand-picked.", caption_style))

    doc.build(story)
    return buf.getvalue()
