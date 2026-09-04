# 5-Minute Pitch — Talking Points

Selection has no resume screen or aptitude test — it's the repo, this video,
and a panel walkthrough where you defend the architecture. So the video's
job is narrow: prove the thing works, and prove you understand *why* it's
built this way, not just that it runs. Record the demo section by actually
running the dashboard on screen — don't describe it, show it.

## 0:00–0:35 — The problem (say this fast, don't over-explain)

"Every merchant on every payment gateway runs the same loop every day: does
the money that landed in the bank actually match the invoices on the books?
Right now that's a person with two spreadsheets and a calculator, and the
moment a bank narration gets truncated or a fee gets deducted, they're stuck
manually investigating. I built an agent that closes that loop and reports
exactly how well it's doing — not a demo number, a measured one."

## 0:35–1:30 — Why the obvious approaches fail

Say this compactly, naming the pattern, not every tool:

"There are two failure modes in this space today. Rule-based tools —
BlackLine, Trintech, Modern Treasury — do deterministic matching, and the
moment something's not an exact match, it gets dumped into a human queue
with zero explanation. They also don't really understand Indian rails —
IMPS/NEFT/UTR narrations, or TDS and GST-on-fee deductions. On the other
end, the newer AI-native accounting tools just feed raw numbers into an LLM
— which is fast to build but impossible to audit, because LLMs can
hallucinate arithmetic, and no CFO will sign off on that."

"So I built the middle: a deterministic core that proves every match
mathematically, and an LLM that's used only as a narrow, read-only
investigator for the residual cases the deterministic core genuinely can't
resolve — and even then, it only proposes, it never writes to the ledger."

## 1:30–2:15 — Architecture, in one breath

Walk the layer diagram from the README (have it on screen, or draw it live):

"Layer one is exact reference matching. Layer two is a deduction-hypothesis
engine — instead of just flagging a variance, it tests known formulas: a
gateway fee plus 18% GST on that fee, or a TDS deduction, and if one fits,
it closes the record *and shows the formula*. Layer three handles batched
settlements — one payout covering several invoices — by anchoring on
whichever reference codes matched exactly and using the leftover narration
text to identify the rest, rather than blindly searching the whole ledger,
which I found creates false-positive collisions fast. Layer four tracks
partial payments. Only what survives all four goes to layer five — the
Tier-1 Investigator — which proposes a match with a plain-language rationale
and a confidence score, and every one of its proposals sits as pending
confirmation until a human clicks Confirm."

## 2:15–4:00 — Live demo (screen-record the actual Streamlit app)

1. Open the dashboard, point at the five metric tiles for 3 seconds each:
   auto-match rate, resolved rate, precision, recall, deduction-hypothesis
   accuracy. Say the numbers out loud once.
2. Click into the layer breakdown chart: "84% of this volume never touches
   an LLM at all — that's what makes the match rate auditable."
3. Open the **Pending confirmation** tab, pick one row, read its rationale
   out loud, and click Confirm. Say explicitly: "That's the guardrail —
   nothing closes until I click this."
4. Open the **Exceptions** tab and read one rationale — pick one where the
   agent correctly declined to guess. "It's not forcing a match here — it's
   telling a human exactly why it couldn't, which is the opposite of what
   the exception-dump tools do."
5. (Optional, if time) Click Regenerate with a different seed to show the
   metrics are computed live, not hard-coded.

## 4:00–4:40 — Results recap + roadmap

"On this batch: 85% auto-matched with zero LLM calls, 90% resolved overall,
100% precision, 98% recall, and the deduction engine got every fee/GST/TDS
formula right. The two things I'd build next: letting the investigator
propose a full multi-invoice batch instead of just its single best member,
and pulling a merchant's actual fee schedule instead of testing against a
fixed rate list."

## 4:40–5:00 — Close

"This is the AI Finance Controller track brief, solved the way I think a
CFO would actually want it solved — provable where it can be, and honest
about the LLM's role where it can't."

---

## Panel Q&A prep (they said "defend your architectural decisions")

**"Why not just let the LLM do all the matching, it'd be simpler?"**
Because it's not auditable — an LLM can be right 95% of the time and still
be unacceptable for a ledger, because you can't prove *which* 5% it got
wrong without redoing the work by hand anyway. The deterministic core means
84%+ of the match rate is provable with a calculator, not a black box.

**"Why does the LLM never get write access?"**
Because the failure mode of a wrong auto-close is much worse than the
failure mode of a slow human confirmation. A finance controller can tolerate
"the agent flagged this for review" all day; they cannot tolerate "the agent
silently closed the wrong invoice."

**"How did you validate the match rate is real and not overfit to your own
synthetic data?"**
The ground truth and deduction-truth files are generated independently and
never read by the agent — only by the scoring code. The generator also
deliberately includes cases designed to break naive approaches: garbled
references, batched settlements, and genuinely unmatched records the agent
must correctly leave alone rather than force-match.

**"What breaks this at real scale?"**
The subset-sum layer is bounded to combinations of 3 within a date window to
keep it fast and avoid false-positive collisions between similarly-formatted
reference codes (this happened during development — see README limitations).
At ledger scale you'd want the anchor-first approach used everywhere instead
of a bounded brute-force search.

**"Why gateway fee + GST specifically, why TDS?"**
Those are the two deduction mechanics that actually show up on Indian
settlement files and that generic Western reconciliation tools don't model —
it's a direct answer to the "domestic payment-rail blindspot" gap in the
market.
