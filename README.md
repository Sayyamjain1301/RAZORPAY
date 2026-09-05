# Payment Reconciliation Agent

**Live app:** https://sammyunfiltered.streamlit.app/
**Project overview page:** https://sayyamjain1301.github.io/RAZORPAY/

**Track:** AI Finance Controller — Razorpay AI Buildathon 2026
**Brief:** *"Build an agent that closes one finance-ops loop across a 50+ record batch of synthetic data, reporting its match rate."*

This agent closes the **invoice-to-settlement reconciliation loop** — the same
loop every merchant on every payment gateway runs every day: does the money
that landed in the bank actually match the invoices/orders on the books?

On the included 60-invoice / 60-settlement synthetic batch, it currently
reports:

| Metric | Result |
|---|---|
| Auto-match rate (zero LLM calls) | **85.0%** |
| Resolved rate (auto + LLM-proposed) | **90.0%** |
| Precision (of everything proposed) | **100%** |
| Recall (of everything that truly had a match) | **98.2%** |
| Fee/GST/TDS deduction-hypothesis accuracy | **100%** |

Regenerate the data with a different seed/size and these numbers will move
slightly — that's the point: they're measured, not hand-picked.

## Why this design, not the obvious one

The reconciliation space already has three tiers of players, and each has a
specific, well-known failure mode:

- **Enterprise rule engines** (BlackLine, Trintech, HighRadius) and
  **payment-ops tools** (Modern Treasury, Ledge, Nilus, Bluecopa) do
  deterministic, threshold-based matching. The moment an amount is off by a
  few rupees or a bank narration format changes by one delimiter, the record
  gets dumped into a human queue with no explanation — "unmatched" is not an
  answer, it's a shrug. They're also mostly built around US rails (ACH,
  Stripe payout batches) and don't understand UTR-style IMPS/NEFT/RTGS
  narrations or Indian TDS/GST deduction math at all.
- **AI-native accounting startups** (Numeric, Vic.ai, Puzzle, Digits) swing
  the other way: raw transactions go straight into an LLM. That's fast to
  demo and impossible to audit — LLMs are non-deterministic and can
  hallucinate arithmetic, which is exactly what a CFO cannot sign off on.

This agent is built as the missing middle: **a deterministic core that
proves its matches mathematically, with an LLM used only as a narrow,
read-only investigator for the residual cases the deterministic core
genuinely can't resolve on its own.**

## Architecture

```
                     ┌─────────────────────────────────────────────┐
                     │              Settlement batch                │
                     └───────────────────────┬───────────────────────┘
                                              ▼
   Layer 1  Exact reference match      normalize narration, look for an
                                        invoice's reference code as a
                                        clean substring
                                              │
                                              ▼
   Layer 2  Deduction-hypothesis       does the variance equal a known
            engine                     gateway-fee(+18% GST on the fee) or
                                        TDS formula? if yes, close it and
                                        SHOW the formula, don't just note
                                        "amounts differ"
                                              │
                                              ▼
   Layer 3  Batch / subset-sum +       one settlement covering N invoices?
            anchored completion        anchor on whichever codes matched
                                        exactly, then use the LEFTOVER
                                        narration text to identify the
                                        rest — never a blind ledger-wide
                                        subset-sum (that collides constantly
                                        when codes share a prefix format)
                                              │
                                              ▼
   Layer 4  Partial-payment            settlement < remaining balance and
            accumulation               reference matches → apply as a
                                        partial payment, keep invoice open
                                              │
                              ┌───────────────┴───────────────┐
                              │ resolved?                       │
                         yes  │                            no  │
                              ▼                                 ▼
                    ┌──────────────────┐          ┌──────────────────────────┐
                    │   AUTO-CLOSED    │          │ Layer 5: Tier-1 Exception │
                    │  (deterministic, │          │ Investigator (LLM, or a   │
                    │  100% provable,  │          │ rule-based fallback if no │
                    │  zero token cost)│          │ API key is configured)    │
                    └──────────────────┘          └────────────┬──────────────┘
                                                                 ▼
                                                    ┌─────────────────────────┐
                                                    │ PENDING CONFIRMATION     │
                                                    │ (proposal + confidence + │
                                                    │  plain-language          │
                                                    │  rationale — NEVER       │
                                                    │  auto-written to the     │
                                                    │  ledger)                 │
                                                    └─────────────────────────┘
```

On this run, the layer breakdown was:

| Layer | Settlements resolved |
|---|---|
| Exact reference + deduction engine | 38 |
| Exact reference + partial payment | 7 |
| Exact reference batch + deduction engine | 2 |
| Anchored batch completion | 2 |
| Amount/date subset-sum (no usable reference at all) | 2 |
| LLM / Tier-1 investigator (pending confirmation or exception) | 9 |

**84%+ of volume never touches an LLM at all.** That's deliberate: every one
of those matches can be reproduced by a human with a calculator, which is
what makes the reported match rate meaningful rather than a black box.

### The guardrail, and why it's the actual pitch

The Tier-1 Investigator (`recon_agent/llm_reasoner.py`) is the **only**
module that talks to an LLM, and it is scoped narrowly on purpose:

- It only ever sees one settlement plus its top 2-3 candidate invoices
  (already scored deterministically) — never the full ledger.
- It cannot write to invoice state. It returns a proposal:
  `{chosen_invoice_id, confidence, rationale}`.
- Every proposal surfaces in the dashboard as **pending confirmation** and
  stays that way until a human clicks Confirm. Nothing closes a ledger entry
  without a human in the loop.
- If no `GEMINI_API_KEY` is set, or the API call fails for any reason, it
  transparently falls back to a deterministic, explainable rule-based
  investigator — the pipeline never crashes and never silently blocks.

## Synthetic data

`data_gen.py` generates a batch that mirrors real Indian settlement-file
mess, not a clean textbook case:

- exact reference matches, sometimes with a gateway fee (1.8-3%) + 18% GST
  *on that fee* deducted, or a TDS deduction (1/2/10%) instead
- garbled reference codes (truncated, typo'd, re-cased, re-spaced, prefixed
  with noise) that need fuzzy/anchored reasoning, not regex
- batched settlements covering 2-3 invoices in one payout
- partial payments split across two settlements
- reference codes dropped from the narration entirely (amount+date only)
- a handful of genuinely unpaid invoices (no settlement should ever be
  invented for these) and unexplained credits (no invoice should be invented
  for these either) — the agent is scored on correctly leaving these alone,
  not just on how much it matches

A hidden `ground_truth.csv` and `deduction_truth.csv` (never read by the
agent itself) let the dashboard report precision/recall and deduction-formula
accuracy objectively instead of just asserting them.

## Project layout

```
payment-recon-agent/
├── data_gen.py              synthetic invoice + settlement generator
├── recon_agent/
│   ├── matcher.py            layers 1-4 (deterministic) + orchestration + scoring
│   └── llm_reasoner.py       layer 5 (Tier-1 Exception Investigator), bounded & read-only
├── app.py                    Streamlit dashboard
├── run_cli.py                terminal-only runner (no browser needed)
├── data/                     generated CSVs (invoices, settlements, ground truth)
└── requirements.txt
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Generate the synthetic batch (already included in `data/`, but you can
regenerate with a different size/seed):

```bash
python data_gen.py --n 60 --seed 42
```

Run the dashboard:

```bash
streamlit run app.py
```

Run from the terminal instead (no browser):

```bash
python run_cli.py            # uses live Gemini API if GEMINI_API_KEY is set
python run_cli.py --no-llm   # force the deterministic rule-based fallback
```

To enable the live LLM investigator, set your API key first:

```bash
export GEMINI_API_KEY=...   # from Google AI Studio / Cloud Console
```

Without it, everything still runs end-to-end — layer 5 just uses the
rule-based fallback instead of calling out to Gemini, and says so in its
rationale.

## Console UX

Beyond the matching pipeline itself, the Streamlit console (`app.py`) adds:

- **Cold-start auto-seed** — a fresh deploy with no `data/` yet (Streamlit
  Cloud, a fresh clone) seeds a starter batch automatically instead of
  showing a blank info banner.
- **Take the tour** (sidebar) — seeds a batch with every scenario type,
  runs the real pipeline, and walks the actual dashboard tab-by-tab with a
  floating step-through overlay. Never a staged/fake run — the same
  `run_pipeline_once()` the manual Run button uses.
- **Pipeline flow diagram** — an animated 5-node visualization shown right
  after a run finishes, each node counting up to its real settlement count
  in order, so the "cheap deterministic layers first, LLM only on the
  residue" thesis is the first thing you see, not a line in a README.
- **Assistant as a control surface** — the floating AI assistant (present
  on every page) doesn't just answer questions; a request like *"show me
  every pending confirmation under 70% confidence"* actually applies that
  filter on the Reconciliation tab, switching tabs if needed. Narrow by
  design: it only ever sets state to something backed by real, currently-
  present data.
- **KPI sparklines** — each Reconciliation-tab KPI tile shows a small trend
  line across recent runs, built from the same run-history file the
  Overview tab's trend chart already uses.
- **Motion that carries information** (`recon_agent/motion.py`) — every
  animation marks a real state change, never decoration. The headline one:
  the hero KPI tweens from the *previous run's* rate to the current one, so
  the number visibly travels the distance the run actually moved it (a
  0 → current count-up would hide that). Card and row groups reveal in
  reading order with a short stagger, and each is gated to fire once per
  run — moving a filter or switching tabs shows everything flat rather than
  replaying. `prefers-reduced-motion` disarms all of it.

## Known limitations / roadmap

- The investigator currently proposes exactly one invoice per unresolved
  settlement. A settlement that is a *partially*-identified batch (one
  reference confirmed, two others too garbled for even the anchored-completion
  heuristic) surfaces only its most confident member rather than the full set —
  next step is a multi-candidate proposal from the investigator.
- Deduction hypotheses are a fixed list of known Razorpay-style gateway fee
  tiers + GST + common TDS rates. A production version would pull the
  merchant's actual fee schedule instead of testing against a fixed list.
- Subset-sum search is bounded to combinations of up to 3 invoices within a
  22-day window to keep it fast and to avoid false-positive collisions
  between similarly-formatted reference codes — sufficient for this batch
  size, would need tightening (or a smarter anchor-first search) at
  ledger scale.
