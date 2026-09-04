# PRD — Razorpay Settlement Reconciliation Agent
### "AI Finance Controller" track · Razorpay AI Buildathon 2026

> **How to use this doc:** This is a complete build spec. Read it fully before writing code.
> Build in the phase order given in §12. Deadline is hard: **applications close 5 Sept 2026.**
> Everything in §4 (domain constants) is real, verified data — do not invent alternatives.

---

## 1. The brief we are answering

Official track text, verbatim:

> **AI Finance Controller — Run the books and the cash position**
> Build an agent that closes one finance-ops loop across a 50+ record batch of synthetic data,
> reporting its match rate and the exceptions it could not resolve.
>
> **why now:** The 2026 builder consensus: verification capacity, not generation speed, is the
> bottleneck. Reconciliation, settlement and forecasting are still done by hand.
>
> **the bar:** Throughput plus measured accuracy plus an honest exception list.
> One cherry-picked match proves nothing.

Submission deliverables: public GitHub repo + README + 5-minute pitch video + architecture
walkthrough at a panel interview where every architectural decision must be defended.

**The bar decomposes into three gradeable things. All three are mandatory:**

| Requirement | What it means concretely |
|---|---|
| **Throughput** | records/sec, wall-clock, p50/p95 latency, cost per 1,000 records in real ₹/$ |
| **Measured accuracy** | multi-seed mean ± std, held-out set, ablation per layer, calibration |
| **Honest exception list** | closed-vocabulary reason codes, why-not-matched with evidence, and a `MODEL_COULD_NOT_DECIDE` bucket |

---

## 2. What we are building

**A three-way reconciliation agent that closes the invoice → settlement → bank-credit loop
for an Indian merchant on Razorpay, and ships a verification artifact with every decision.**

Three data sources, not two:

```
  Source A: merchant invoices/orders (internal books)
       ↕
  Source B: Razorpay settlement recon report (gateway truth, net of fees/tax)
       ↕
  Source C: bank statement credits (what actually hit the current account)
```

**Why three sources is the whole point:** Razorpay's `settlement_utr` (e.g. `1568176960vxp0rj`,
a unix timestamp + 6 alphanumerics) is **not** a bank UTR (NEFT is 16-char `HDFCN26015 12345678`,
RTGS is 22-char). So B↔C cannot be joined on the reference field, and the merchant is forced into
amount + date + narration-parsing territory. Razorpay's own merchant playbook names exactly this:
*"Missing UTRs, so payouts cannot be matched to bank credits. Unexplained deductions finance
teams cannot trace."* We solve the problem their own docs admit exists.

### Product thesis (say this in the pitch, build to it)

> The agent does not just match transactions. **Every match ships with its own verification
> artifact** — an evidence chain a human can check in seconds. Everything it cannot verify is
> escalated with a reason code, not guessed. The exception list is the feature, not the apology.

---

## 3. Non-goals — do not build these

- ❌ No cash forecaster, no tax-line matcher, no settlement Q&A chatbot. The brief says close
  **one** loop and prove it. Breadth reads as shallowness.
- ❌ No React/Next.js frontend. Streamlit only. A rewrite is a bad risk-adjusted trade here.
- ❌ No live Razorpay API integration. Synthetic data only, but shaped exactly like the real API.
- ❌ No database. CSV/JSONL on disk is correct at this scale.
- ❌ No authentication, no multi-tenant, no deployment infra.
- ❌ Do not chase a 100% match rate. A 100% claim on self-generated data is a liability.

---

## 4. Domain constants — REAL VALUES, verified. Do not invent substitutes.

### 4.1 Money representation (non-negotiable)

**All money is integer paise. No floats anywhere in matching logic.** Razorpay's API is
paise-native (`amount: 100000` = ₹1,000.00). Float money in a reconciliation engine is a defect
a finance judge will catch. Format to rupees only at the display layer.

### 4.2 Razorpay Settlement Recon API schema (mirror these field names exactly)

Endpoint shape: `GET /v1/settlements/recon/combined?year=YYYY&month=MM&day=DD`

```json
{
  "entity_id": "pay_DEXrnipqTmWVGE",
  "type": "payment",              // payment | refund | transfer | adjustment
  "debit": 0,
  "credit": 97100,
  "amount": 100000,
  "currency": "INR",
  "fee": 2900,
  "tax": 0,
  "on_hold": false,
  "settled": true,
  "created_at": 1567692556,
  "settled_at": 1568176960,
  "settlement_id": "setl_DGlQ1Rj8os78Ec",
  "posted_at": null,
  "credit_type": "default",
  "description": "...",
  "notes": "...",
  "payment_id": null,
  "settlement_utr": "1568176960vxp0rj",
  "order_id": "order_DEXrnRiR3SNDHA",
  "order_receipt": null,
  "method": "card",               // card | upi | netbanking | wallet | emi
  "card_network": "MasterCard",
  "card_issuer": "KARB",
  "card_type": "credit",
  "dispute_id": null
}
```

Settlement (payout) entity — note **plural** `fees`/`tax` here vs singular at line level:

```json
{ "id": "setl_DGlQ1Rj8os78Ec", "entity": "settlement", "amount": 9973635,
  "status": "processed", "fees": 471699, "tax": 42070,
  "utr": "1568176960vxp0rj", "created_at": 1568176960 }
```

ID prefixes to use: `pay_`, `rfnd_`, `order_`, `inv_`, `setl_`, `setlod_` (on-demand), `disp_`.

### 4.3 Fee / tax formulas (current as of 2026 — older blog posts have stale rates)

| Deduction | Rate | Applied on |
|---|---|---|
| MDR — cards, netbanking, wallets, UPI, RuPay debit | **2.00%** | gross txn amount |
| MDR — debit-card EMI | **1.00%** | gross |
| MDR — credit card on UPI | **2.15%** | gross |
| MDR — AMEX/Diners, corporate cards, cardless EMI, Pay Later, international cards | **3.00%** | gross |
| MDR — international wallets / local methods | **3.50%** | gross |
| **GST** | **18%** | **on the MDR fee only, never on the invoice amount** |
| TDS u/s 194-O | **0.10%** | gross (was 1% pre-Oct-2024). 5% u/s 206AA if no PAN. ₹5L/FY threshold |
| GST TCS u/s 52 | **0.50%** | net taxable value (was 1% pre-July-2024) |

Worked example a Razorpay judge will instantly recognise:
`₹10,000 card payment → MDR ₹200 → GST ₹36 → net settled ₹9,764`

**The refund trap (build a feature around this):** on a normal refund, Razorpay does **not**
reverse the fee charged at capture. A fully refunded ₹10,000 order still costs the merchant
₹236 permanently. Most merchants never book this. See §8.4.

### 4.4 Settlement timing

- Default cycle **T+2 working days** from capture (cards); UPI T+1. Bank holidays excluded.
- `settlement.processed` webhook fires at **transfer initiation** — actual bank credit lags up to 3 hours.
- Instant settlement: **₹5L cap per IMPS transaction** → a larger instant payout arrives as
  **multiple separate bank credits**. This is a real recon headache; generate it.
- Smart settlement ₹5L–₹50Cr goes via RTGS as a single credit.
- Banks typically do not credit on Sundays/holidays → timing exceptions cluster there.

### 4.5 UTR / reference formats (Source C)

| Rail | Format | Example |
|---|---|---|
| NEFT | 16-char: `BANK4` + `N` + year3 + **Julian date3** + seq8 | `HDFCN2601512345678` |
| RTGS | 22-char: `BANK4` + `RC` + `YYYYMMDD` + seq8 | `HDFCRC2026011500001234` |
| IMPS | RRN, 12–16 numeric | `986512345678` |
| UPI | RRN, 12-digit numeric | `854977234911` |
| Cards | 6-digit `auth_code`; `arn` on refunds | `299196` |

### 4.6 Bank narration patterns (generate these; parse these)

```
NEFT   NEFT CR:HDFC2268012345678 ABC CORP INV-2024-001
RTGS   RTGS CR:SBIN2268001234567 XYZ LTD ADVANCE          (Axis omits "CR:")
UPI    UPI/P2M/123456789012/john@oksbi/ORDER-891
IMPS   IMPS/9876543210/RAHUL KUMAR/9876                    (names truncated 10-15 chars)
NACH   NACH/BATCH-20260315-001/HDFC0000001                 (aggregates 200-500 debits)
RZP    RAZORPAY SETTLEMENT 1568176960vxp0rj                (gateway payout credit)
```

**Delimiter conventions differ per bank — implement all three:**
HDFC uses `/` forward slashes · ICICI uses `-` hyphens · Axis uses ` ` spaces.

### 4.7 Exception reason codes — CLOSED VOCABULARY

There is no canonical published taxonomy in this market; every vendor describes exception
management conceptually and none publishes concrete categories. **Publishing a crisp MECE
taxonomy is itself differentiation.** Use exactly these, no free-text categories:

| Code | Meaning | Auto-resolvable |
|---|---|---|
| `TIMING_LAG` | settlement cycle / cutoff / weekend, in-transit | usually |
| `FEE_VARIANCE` | MDR deducted at source | usually |
| `TAX_DEDUCTION` | GST-on-fee / TDS 194-O / TCS 52 | usually |
| `ROUNDING` | sub-rupee | always |
| `PARTIAL_SETTLEMENT` | one invoice paid across multiple credits | sometimes |
| `BATCH_SPLIT` | one credit covering N invoices | sometimes |
| `INSTANT_SETTLEMENT_SPLIT` | ₹5L IMPS cap fragmenting one payout | sometimes |
| `DUPLICATE` | double-posted / retried webhook | detect, never match |
| `MISSING_REFERENCE` | no usable reference in narration | rarely |
| `IDENTIFIER_MISMATCH` | garbled/truncated reference | sometimes |
| `REFUND_OFFSET` | refund netted against a later settlement batch | sometimes |
| `UNEXPLAINED_CREDIT` | credit with no corresponding invoice | never — escalate |
| `GENUINELY_UNMATCHED` | invoice with no money against it | never — escalate |
| `MODEL_COULD_NOT_DECIDE` | **our** limitation, not the data's | never — escalate |

**`MODEL_COULD_NOT_DECIDE` is mandatory and is the single highest-trust feature in the build.**
Separating our failure from the data's failure is what almost nobody does.

---

## 5. Architecture

```
 ingest ─► normalize ─► L1 exact ref ─► L2 deduction engine ─► L3 batch/subset ─► L4 partial
                                                                                      │
                                                        ┌─────── resolved? ───────────┤
                                                   yes  │                        no   │
                                                        ▼                             ▼
                                              AUTO-CLOSED                  L5 Exception Investigator
                                          (deterministic, ₹0,               (LLM, bounded, read-only)
                                           replayable, provable)                      │
                                                        │                             ▼
                                                        │                   PENDING CONFIRMATION
                                                        │                  (proposal + evidence +
                                                        │                   reason code, no write)
                                                        ▼                             ▼
                                                   ┌────────────────────────────────────┐
                                                   │   AUDIT LOG (append-only JSONL)     │
                                                   └────────────────────────────────────┘
```

### Layer contracts

**L0 — Normalize.** Parse narrations per bank dialect, extract candidate references
(tokenize on `/ - + space`, keep tokens ≥5 chars containing digits), normalize to uppercase
alphanumeric. Detect and quarantine duplicate rows (same amount + date + narration hash) as
`DUPLICATE` before matching — never silently match a duplicate.

**L1 — Exact reference match.** Reference code appears verbatim as a substring of the
normalized narration. If a token matches ≥2 open invoices ambiguously, do **not** pick one —
demote to L5 with both candidates.

**L2 — Deduction hypothesis engine.** When amounts differ, test known formulas from §4.3
against the delta before giving up: MDR at each published rate, MDR+GST, TDS, TCS, and
combinations. Tolerance: `max(100 paise, 0.06% of target)`. On a fit, close deterministically
and **record the formula**, not just a boolean. This is the layer that answers the industry's
universal blind spot (NetSuite's AI matcher requires exact amounts and cannot handle
bank-deducted fees at all).

**L3 — Batch / subset-sum, anchor-first.** If ≥1 reference is confirmed but the total doesn't
reconcile, search combinations that are **supersets of the confirmed members**, scoring added
members by similarity against the *residual* narration text (narration minus the already-matched
codes). Blind ledger-wide subset-sum is forbidden: with prefix-shaped reference codes it produces
constant false-positive collisions. If two candidate combos score within 10 points, defer to L5.

**L4 — Partial payment accumulation.** Track `remaining_paise` per invoice; a credit smaller
than the remaining balance with a confirmed reference reduces it and leaves the invoice open.

**L5 — Tier-1 Exception Investigator (the only LLM call in the system).**
- Input: one unresolved record + top-3 deterministically-scored candidates + computed deltas.
- Never sees the full ledger. Never computes arithmetic that matters. Never writes state.
- Output: `{chosen_id | null, confidence, reason_code, rationale}` — reason_code must come
  from §4.7's closed vocabulary.
- **Every L5 output is `pending_confirmation`.** No exceptions, regardless of confidence.
- Graceful degradation: if `ANTHROPIC_API_KEY` is absent or the call fails, fall back to a
  deterministic rule-based investigator and label the source honestly in the output.
- Model must be **pinned by version** (env `RECON_LLM_MODEL`), logged with every decision.

### Guardrail (this is the pitch's spine)

Deterministic layers auto-close. Anything the LLM touches is a **proposal** requiring a human
click. State it as: *"the LLM explains and recommends; it never mutates the ledger."*
The 2026 audit guidance is explicit that confidence scores alone are no longer accepted as an
audit trail — plain-language reasoning is required.

---

## 6. Synthetic data generator spec

`data_gen.py` — must be **written and frozen before any matcher tuning**, and must accept
`--seed` and `--n`. State this separation in the README; it is the cleanest possible answer to
"did you overfit to your own data?"

Generate three files (plus two hidden truth files the agent never reads):

- `invoices.csv` — `invoice_id, customer, gstin, invoice_date, amount_paise, reference_code, method`
- `settlements.csv` — the §4.2 recon schema, one row per payment/refund/adjustment
- `bank_statement.csv` — `txn_id, value_date, credit_paise, debit_paise, narration, bank`
- `ground_truth.csv` *(hidden)* — invoice_id → settlement entity_ids → bank txn_ids
- `deduction_truth.csv` *(hidden)* — entity_id → applied formula + rate

**Scenario mix (target proportions, ±3%):**

| Scenario | Share | Tests |
|---|---|---|
| Clean match, MDR + GST deducted | 30% | L2 |
| Clean match, no deduction | 12% | L1 |
| TDS 194-O deducted | 8% | L2 |
| Garbled reference (truncate / typo / case / spacing / prefix-noise) | 10% | L1→L5 |
| Reference dropped entirely from narration | 6% | L3 |
| Batched payout covering 2–3 invoices | 8% | L3 |
| Partial payment split across 2 credits | 7% | L4 |
| Instant-settlement ₹5L split into multiple credits | 4% | L3 |
| Refund netted against a later settlement | 5% | `REFUND_OFFSET` |
| Duplicate row (retried webhook) | 3% | L0 |
| Weekend/holiday timing lag | 3% | `TIMING_LAG` |
| Genuinely unpaid invoice (no money exists) | 2% | must NOT be matched |
| Unexplained credit (no invoice exists) | 2% | must NOT be matched |

**Adversarial cases — inject deliberately, they are the credibility layer:**
- near-duplicates: same amount, same day, different counterparty
- transposed digits (₹5,431 vs ₹5,341)
- amounts differing only by rounding
- date-boundary items straddling a settlement cutoff
- **honeypots**: records engineered to be *plausibly but wrongly* matchable. Report how many
  the agent took the bait on. This number belongs in the README.

Use realistic Indian merchant names, real GSTIN check-digit format, and Indian amount grouping.

---

## 7. Evaluation harness spec

`evaluate.py` — runs the agent across multiple seeds and emits `metrics.json` + a markdown table.

### 7.1 Accuracy

- Precision, recall — but **optimize recall subject to a precision floor, not F1.** In
  reconciliation a false match silently corrupts a ledger; a missed match lands in a queue a
  human was already reading. Say this explicitly — the asymmetry is domain understanding.
  If a single number is needed use **Fβ with β < 1** (precision-weighted).
- **Headline metric: auto-approval rate at fixed precision.**
  *"X% of the batch cleared automatically at ≥99% precision; Y% went to the exception queue."*
  One number encoding throughput, accuracy and honesty at once.
- **Risk–coverage curve.** coverage = fraction decided rather than escalated (sweep τ);
  selective risk = error rate among accepted only.
- **AUGRC** (Area Under Generalized Risk Coverage), bounded [0, ½], reported as
  **"average risk of undetected failures."** Prefer it over AURC, which violates monotonicity.
- **Calibration: ECE** with adaptive (equal-count) bins, plus **Brier score**.
  `ECE = Σ_m (|B_m|/n) · |acc(B_m) − conf(B_m)|`
  Never report ECE without accuracy beside it — a constant predictor scores ECE=0.
- **Cluster-level metrics** where a credit maps to several invoices: **B³ precision/recall**,
  and report **overclustering (OCE)** and **underclustering (UCE)** error separately.

### 7.2 Statistical honesty (this is what the brief's "cherry-picked" line is testing)

- Run **≥5 seeds**, report **mean ± std**, never a single run.
- Generate a **large batch (n≈2,000)** for statistics; use the 50–60 record batch as the
  live demo instance. At n=60 confidence intervals are wide — **say so in the README.**
- Report 95% CI as point estimate ± 2 SD.
- **Held-out shift test:** evaluate on a batch with counterparties/banks/narration dialects
  absent from any tuning batch, and report the metric *delta*. A small drop honestly reported
  beats a hidden one.
- **Ablation table:** disable one layer at a time; report each layer's marginal contribution to
  match rate *and* to cost. If a layer contributes ≈0, report it and consider cutting it.
- **Decision determinism (DecDet):** re-run 5× on identical input, report % of records whose
  final decision is identical. Target ≥95%. This answers "would this survive an audit replay?"

### 7.3 Throughput & cost

- records/sec end-to-end; wall-clock for the 50-record demo batch
- latency **p50 / p95** (never a bare mean)
- **cost per 1,000 records in ₹ and $**, priced at published API rates, with input/output token
  counts shown
- **cost split**: `X% resolved deterministically at ₹0` vs `Y% consumed N tokens` — this proves
  tokens are spent asymmetrically on the hard residual, not sprayed at everything
- scaling curve at n = 50 / 500 / 5,000

### 7.4 The one-slide scorecard (build this as a rendered artifact in the README)

```
BATCH  n=2000, 5 seeds, frozen generator  [50-record batch shown live in demo]

THROUGHPUT   records/sec ....  X      wall-clock (50) ..... Xs
             p50 / p95 ......  X/X    cost per 1,000 ...... ₹X / $X
             deterministic share ..... X% at ₹0 inference cost

ACCURACY  @ τ = 0.9X
             auto-approval rate ...... X%     precision @ τ ... X%  (95% CI ±X)
             recall .................. X%     false matches ... X
             AUGRC (avg risk of undetected failure) ......... 0.0X
             ECE (adaptive) .. 0.0X   Brier ... 0.0X   DecDet ... X%
             honeypots taken ......... X of Y

EXCEPTIONS   X records (X%) — every one reason-coded
             TIMING_LAG ....... X     MISSING_REFERENCE .... X
             FEE_VARIANCE ..... X     UNEXPLAINED_CREDIT ... X
             PARTIAL_SETTLEMENT X     GENUINELY_UNMATCHED .. X
             MODEL_COULD_NOT_DECIDE ... X    ← ours, not the data's

             matched X + exceptions Y = 2000 ✓
```

The final two lines win this track: a self-attributed failure category, and arithmetic that closes.

---

## 8. UI spec — a work queue, not a dashboard

### 8.1 The structural rule

Serious 2026 finance consoles (Ramp, Brex, Stripe) **invert the dashboard**: work queues take
prime real estate, analytics sit one level down; tables are primary content and charts are
summaries. Build accordingly.

**Layout — three panes:**

```
┌──────────────────────────────────────────────────────────────────────┐
│  thin KPI strip: auto-match % (dominant) · exceptions · ₹ at risk     │
├────────────┬─────────────────────────────────┬───────────────────────┤
│ FILTER     │  EXCEPTION QUEUE (dense table)  │  REVIEW PANEL         │
│ RAIL       │  reason code · age · ₹ · conf   │  side-by-side match   │
│            │                                 │  evidence chain       │
│ status     │  [row selected] ───────────────►│  gross→net waterfall  │
│ conf band  │                                 │  rejected candidates  │
│ reason code│                                 │  ✓ Accept  ✎ Edit  ✗ │
│ amount     │                                 │                       │
│ age        │                                 │                       │
├────────────┴─────────────────────────────────┴───────────────────────┤
│  st.bottom — bulk action bar (appears only when rows are selected)    │
└──────────────────────────────────────────────────────────────────────┘
```

Tabs: **Queue · Matched · Rules · Metrics · Audit log**

### 8.2 The hero screen: gross-to-net waterfall

When a ₹98,412.37 credit lands against ₹1,00,000 of invoices, do **not** print "variance".
Render the decomposition, each line individually confirmable:

```
  Invoice total                    ₹1,00,000.00
  − MDR @ 2.00% (card)                ₹2,000.00   ✓ matches published rate
  − GST @ 18% on fee                    ₹360.00   ✓ formula verified
  − TDS u/s 194-O @ 0.10%               ₹100.00   ✓ threshold met
  − rounding                              ₹0.37
  ─────────────────────────────────────────────
  Expected credit                    ₹97,539.63
  Actual credit                      ₹97,539.63   ✓ reconciled
```

This is the single highest-value screen in the product. Competitors fail here: NetSuite's AI
matcher requires exact amounts; HighRadius is publicly criticised for failing on fee/FX cases.

### 8.3 "Why not matched" — every exception must answer it

Never show a bare unmatched row. Each carries: reason code, the **nearest rejected candidate**,
and the **quantified gap**:

> `MISSING_REFERENCE` · closest candidate INV1044 — amount short by 9.2%, which matches no known
> deduction formula (nearest is MDR+GST at 2.36%); settlement date is 11 days outside the T+2
> window. Recommend manual review. *Not auto-matched — this is the data's ambiguity, not a model
> limitation.*

### 8.4 The "found money" panel

A standing tile: **unrecovered MDR + GST on refunded orders this period.** Since Razorpay does not
reverse capture fees on refunds, this is real, permanently lost margin most merchants never book.
Show the ₹ total and the contributing orders. This is a *find money* feature, not a matching
feature — it lands hard in a demo.

### 8.5 Controls that prove the agent is an instrument, not a black box

- **Confidence threshold slider** — drag it, watch coverage vs precision trade off live against
  the risk–coverage curve. Turns the black box into a controllable dial.
- **Rule preview / dry-run** — propose a rule, backtest it against history, show
  would-match / would-mis-match counts *before* activation. Only Modern Treasury ships this.
- **Grouped bulk approval** — cluster N proposals into semantic groups
  ("Razorpay MDR+GST @2.36%, 213 items, all within ₹0.02") and approve per group, not per row.
  Nobody does grouped triage well; reviewing at volume is the top complaint against BlackLine.

### 8.6 Visual specification

- **`font-variant-numeric: lining-nums tabular-nums;`** — highest-leverage CSS line in the app.
  Without it `₹1,111.11` renders narrower than `₹999.99` and every column wobbles.
- **Indian digit grouping**: `₹12,34,567.00`. Naive comma grouping reads as *wrong* to these judges.
- **Right-align** amounts and percentages. **Left-align** dates, IDs, UTRs.
- Use the true minus sign **U+2212 (−)**, not a hyphen. Always show trailing zeros.
- **Confidence as bands, not percentages.** NN/g research found qualitative labels
  ("High confidence") were trusted and acted on more than numeric scores, which "led to confusion,
  skepticism, or disregard." Show `High / Needs review / Uncertain` as the primary signal; put the
  raw number in the detail drawer.
- **Never encode state in colour alone** (~8% of males are red-green colourblind) — pair with
  icon or label.
- **Colour means state, nothing else.** Red = confirmed failure only. Amber = needs attention.
  Cool neutrals (navy/slate/charcoal) for the frame.
- One dominant KPI, not six equal tiles. The dominant one is **auto-match rate**.
- Row density: condensed 40px / regular 48px, sticky header, frozen ID column,
  1px light-grey dividers (not zebra striping — it collides with hover/selected/flagged states).
- Detail goes in the **right sidebar**, never a modal — modals destroy queue context.

### 8.7 Streamlit 2026 APIs to use (these exist, use them)

`st.column_config.ButtonColumn` (inline Accept/Reject per row) · `st.column_config.MarkdownColumn`
(confidence badges in cells) · column-config `alignment` · `st.dataframe(selection=…, lazy=…)` ·
`st.bottom` (pinned bulk bar) · `st.skeleton` and `:shimmer[]` (live agent progress on camera) ·
`st.fragment(parallel=True)` (stream progress without full rerun) · `st.metric(icon=…)` ·
`st.pagination`.

Theme via `.streamlit/config.toml`: `[[theme.fontFaces]]` to load Inter or IBM Plex Sans properly,
plus `baseFontSize`, `metricValueFontSize`, `metricValueFontWeight`, `dataframeHeaderBackgroundColor`,
`borderColor`, and the semantic `redColor`/`greenColor`/`orangeColor` ramps for confidence bands.
**Bump `baseFontSize` before recording** — text that reads fine on a monitor is unreadable in
compressed 1080p video.

---

## 9. Audit log spec

Append-only JSONL, one record per decision, **12 mandatory fields**:

```json
{ "ts": "2026-09-04T11:02:31.442Z", "decision_id": "dec_01H…", "run_id": "run_01H…",
  "user": "sayyam@…", "agent_version": "0.3.1", "model": "claude-…-20260401",
  "inputs": {"entity_id": "pay_…", "source": "settlements.csv#L42"},
  "rule_invoked": "L2.deduction_engine/mdr_gst_v3",
  "reasoning": "MDR 2.00% + GST 18% on fee explains ₹2,360.00 of ₹2,360.37 delta; residual ₹0.37 within rounding tolerance.",
  "output": {"status": "matched", "invoice_ids": ["INV1032"], "reason_code": null},
  "action": "invoice.remaining_paise 100000 → 0",
  "review": {"by": null, "at": null},
  "hash": "sha256:…", "prev_hash": "sha256:…" }
```

Chain each record's `prev_hash` to the previous — tamper-evidence for ~10 lines of code.
Log **which layer decided** every record: deterministic layers are benchmarkable under audit
standards, probabilistic ones are not, and showing the split is the audit story.

Add `replay.py`: re-run a `run_id` from the log with stubbed LLM responses, **failing loudly if
the agent attempts an unrecorded call.** This gives golden-trace regression tests for free.

---

## 10. Project structure

```
razorpay-recon-agent/
├── README.md                    ← scorecard, architecture, honest limitations
├── PRD.md                       ← this file
├── requirements.txt
├── .streamlit/config.toml       ← theme
├── data_gen.py                  ← frozen before tuning; --seed --n
├── evaluate.py                  ← multi-seed, ablation, throughput, calibration
├── replay.py                    ← deterministic replay from audit log
├── app.py                       ← Streamlit work queue
├── recon_agent/
│   ├── __init__.py
│   ├── money.py                 ← integer paise, Indian formatting, U+2212
│   ├── normalize.py             ← L0: narration dialects, tokenizing, dedup
│   ├── deductions.py            ← L2: MDR/GST/TDS/TCS hypothesis engine
│   ├── matcher.py               ← L1–L4 orchestration
│   ├── investigator.py          ← L5: bounded, read-only LLM
│   ├── taxonomy.py              ← closed vocabulary of reason codes
│   ├── audit.py                 ← hash-chained JSONL logger
│   └── metrics.py               ← precision/recall/AUGRC/ECE/B³/DecDet
├── tests/
│   ├── test_deductions.py       ← every §4.3 formula, exact paise
│   ├── test_normalize.py        ← all 3 bank dialects, all 5 rails
│   └── test_no_float_money.py   ← assert no float arithmetic on money paths
└── data/
```

---

## 11. Acceptance criteria

Ship only when every box is true:

- [ ] All money is integer paise; a test asserts no float arithmetic on money paths
- [ ] Synthetic data mirrors §4.2 field names exactly; rates match §4.3 exactly
- [ ] Generator was frozen before matcher tuning, and the README says so
- [ ] Three sources reconciled (invoice ↔ settlement ↔ bank), not two
- [ ] `settlement_utr` ≠ bank UTR join problem is explicitly handled and demoed
- [ ] Every exception carries a §4.7 reason code — zero free-text categories
- [ ] `MODEL_COULD_NOT_DECIDE` exists and is populated
- [ ] Every exception names its nearest rejected candidate and quantified gap
- [ ] `matched + exceptions = N` is displayed and arithmetically true
- [ ] Metrics reported as mean ± std across ≥5 seeds, with CI, plus held-out delta
- [ ] Ablation table present, including any layer with ≈0 contribution
- [ ] Throughput table with records/sec, p50/p95, and cost per 1,000 in ₹ and $
- [ ] Deterministic-vs-LLM cost split shown
- [ ] Honeypot bait-taken count reported
- [ ] DecDet ≥95% over 5 reruns
- [ ] Every L5 output is `pending_confirmation`; no LLM path can close a ledger entry
- [ ] Audit log has all 12 fields, hash-chained; `replay.py` reproduces a run
- [ ] Runs end-to-end with **no** `ANTHROPIC_API_KEY` set (rule-based fallback, labelled)
- [ ] UI is a queue with a right-hand review panel, not a chart dashboard
- [ ] Gross-to-net waterfall renders for any deduction-explained match
- [ ] `tabular-nums` + Indian grouping applied everywhere money appears
- [ ] README states limitations, including CI width at n=60

---

## 12. Build order (ruthless — ~2 days)

**P0 — must ship (day 1).** Without these there is no submission.
1. `money.py` + integer-paise discipline + tests
2. `data_gen.py` with §4.2 schema, §4.3 rates, full scenario mix, honeypots, hidden truth files
3. `taxonomy.py` + `deductions.py` (L2 is the differentiator — build it early)
4. `normalize.py` + `matcher.py` (L0–L4) with the anchor-first rule from §5
5. `investigator.py` (L5) with hard `pending_confirmation` guardrail + no-key fallback
6. `evaluate.py`: multi-seed mean ± std, precision/recall, exception breakdown, closing arithmetic
7. Throughput + cost table

**P1 — the grade-lifters (day 2 morning).**
8. Streamlit work-queue rewrite: three panes, review panel, reason-code filters
9. Gross-to-net waterfall
10. "Why not matched" evidence strings
11. Ablation table + held-out shift test + honeypot count
12. `audit.py` hash-chained log; DecDet over 5 reruns
13. Confidence bands, `tabular-nums`, Indian grouping, theme config

**P2 — only if genuinely ahead (day 2 afternoon).**
14. Confidence threshold slider wired to a live risk–coverage curve
15. Grouped bulk approval
16. AUGRC + ECE + reliability diagram
17. "Found money" refund-leakage panel
18. `replay.py`
19. Rule preview / dry-run

**Then stop coding.** Reserve the final block for README, the scorecard, and recording the video.

---

## 13. Pitch framing (put this in the README too)

The brief's "why now" is the verification-bottleneck thesis, and there is real evidence behind it:
METR's randomised trial found experienced developers were **19% slower** with AI tools while
believing they were 20% faster; DeepMind calls such systems **"conjecture machines"** — cheap to
generate candidates, unchanged cost to validate.

So the pitch is never *"it matches transactions fast."* It is:

> **Every match ships with its own verification artifact. Everything it cannot verify is
> escalated with a reason code, not guessed. And it tells you which failures are the data's
> and which are its own.**

Three lines that should be said out loud in the video:
1. *"84% of this volume never touches an LLM — that's what makes the match rate auditable."*
2. *"The LLM explains and recommends. It never mutates the ledger."*
3. *"This category — `MODEL_COULD_NOT_DECIDE` — is my agent's failure, not the data's. I report it separately."*

---

## 14. Reference — prior prototype

A working v1 exists (Streamlit, 2-source, ~85% auto-match, 100% precision on a single seed).
Worth porting: the deduction-hypothesis engine shape, the anchored batch-completion routine, and
the rule that ambiguous subset-sum matches must defer rather than guess. Everything else —
schema, third source, UI, evaluation harness — is superseded by this document.

**Its central weakness, and why this rewrite exists:** single-seed metrics on self-generated data.
Research on entity-resolution evaluation shows self-generated benchmarks inflate pairwise precision
toward 1.0 and can reverse system rankings outright. A 100% precision claim from one seed is a
liability at a panel interview, not an achievement. Fix it with §7.2.
