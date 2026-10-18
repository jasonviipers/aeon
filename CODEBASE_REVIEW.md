# AEON: codebase and decision-engine review

Review date: October 17, 2026. Scope: `C:\Jason\aeon`.

## 1. Overall judgment

**AEON is a promising experimental trading platform, but I would not yet classify it as a dependable unattended live-trading system.** Its boundaries between data, proposals, deterministic risk, execution, persistence and presentation are sensible. The largest weaknesses are not Python performance or the particular LLM: they are execution-state correctness, protection verification, accounting, and the absence of reproducible evidence that the decision engine adds value.

My decision would be to **keep the architecture, narrow the operational scope, repair safety invariants, and build an evaluation harness before adding more intelligence or markets**. I would not start with Rust, microservices, or a second model.

This is a software assessment, not evidence of profitability or a recommendation to trade particular assets. Passing tests does not establish trading edge or certify exchange integration.

### Scope and limitations

- Reviewed all application modules, root configuration, launcher, packaging, README, hosting guidance and tests. Independent component reviews supplemented the main decision-engine review.
- Initial offline validation: **77 tests passed in 0.90 seconds**. In-memory/fake-exchange probes reproduced gaps below. A final validation checkpoint is recorded at the end.
- Did not run AEON against an exchange or LLM, place orders, inspect `.env`, or read account/database contents. Existing databases were left alone.
- No application implementation was changed by this review. This report is the intended project addition.
- The directory is not a Git checkout, so there is no commit ID. Another process changed files during inspection, including adding Discord notifications. This is a moving-snapshot review, not approval of a frozen release. References may move; relevant function names are included where useful.
- Exchange response guarantees and history-field semantics remain integration questions, not externally verified facts in this assessment.

## 2. How the architecture fits together

```text
Contract specifications and ticker universe
  -> selected spot / perpetual / dated-future markets
  -> candles, indicators, funding and basis snapshot
  -> account + risk status + recent outcomes + previous notes
  -> LLM or heuristic proposal batch
  -> action-by-action deterministic governor
  -> quantization and broker execution
  -> journal, independent monitor, terminal, optional Discord alerts
```

The model call is outside the broker lock, allowing monitoring while it thinks. That is good. However, **serialization is not reconciliation**: a lock does not make cached positions current, exchange acknowledgments final, or independent database writes atomic.

The effective decision engine is `Market` + `LlmBrain`/`HeuristicBrain` + `RiskEngine` + `Agent` + broker. Reviewing only the prompt would miss the most consequential problems.

## 3. Component-by-component assessment

| Element | What is good | What I would improve |
|---|---|---|
| `aeon/__init__.py` | Minimal package surface. | Keep minimal; expose build/version identity in diagnostics. |
| `aeon/__main__.py` | Central wiring, staged modes, live-arming phrase, diagnostics. | Reconcile before decisions; supervise task health; clean up failed startup; add local/authenticated headless controls and single-instance protection. |
| `aeon/config.py` | Dataclasses, defaults, unknown-key rejection, environment secrets. | Finite values, strict list types, positive timing/counts, bounded percentages, consistent stop/expiry settings. Validation is narrower than the loader appears. |
| `config.toml` | Readable centralized policies. | Research/operation presets; budget-is-not-isolation warning; avoid permanent model-price/funding-interval claims. Explicitly document the selected mode. |
| `aeon/okx.py` | Small async client, isolated signing, GET retries, no blind POST retries. | Typed responses, item-level errors, persistent client IDs, recovery lookup, pagination and endpoint contract tests. |
| `aeon/market.py` | Metadata, affordability filters, per-kind quotas, held-market inclusion, bounded concurrency. | Timestamped snapshots, candle policy, warm-up, metadata refresh, spread/depth data, isolated per-market errors. |
| `aeon/feed.py` | WebSocket reconnect/backoff and REST fallback. | Per-instrument freshness, fallback for all kinds, subscription-error handling, stale-cache policy and fault tests. |
| `aeon/indicators.py` | Small pure dependency-free functions. | Reference vectors, finite/short/flat input tests, documented seeding and closed-bar semantics. |
| `aeon/brain.py` | Swappable brains, explicit context, hold option, feedback, cost/latency metadata. | Strict schemas, finite values, batch limits, decision expiry, strategy IDs, structured memory and reproducible evaluation. |
| `aeon/risk.py` | Deterministic limits, clipping, tighten-only stops, daily/drawdown state. | Final rounded-command validation, fresh state, aggregate stop-risk, cost buffers and invariant tests. |
| `aeon/broker.py` | Shared types, simulator, fees/reductions/stops/persistence. | Separate spendable spot cash from marked equity; improve funding/fill/liquidation realism; explicit broker protocol and consistent accounting contracts. |
| `aeon/okx_broker.py` | Isolated derivative orders, attached-protection requests, spot-fee handling, repair intent. | Highest priority: recoverable execution states, owned-fill accounting, immediate state updates, hedge identities, verified protection and confirmed closes. |
| `aeon/agent.py` | Clear orchestration, monitoring during model latency, rejection feedback. | Reconcile each action, invalidate old decisions, durable checkpoints, truthful execution/protection outcomes, correct cycle IDs and degraded state. |
| `aeon/journal.py` | Parameterized SQL, WAL, useful simple tables. | Transactions, durable intents, execution IDs, cycle links, migrations, complete input snapshots, idempotent recovery and estimate corrections. |
| `aeon/tui.py` | Useful risk/positions/events/equity cockpit with confirmations. | Immutable display snapshots, pending/unknown state, verified protection, estimated-value labels and application-level control tests. |
| `aeon/notify.py` | Newly added async delivery, bounded queue, dedupe, throttling, webhook restrictions. | Critical-event priority, observable delivery failure, bounded shutdown/memory, structured severity. In-process alerts cannot detect their own process dying. |
| `tests/` | Fast tests cover meaningful math, parsing, instruments and payloads. | Recovery/fault/replay/invariant coverage. Some tests preserve incorrect behavior, notably spot buying power. |
| `pyproject.toml` | Small dependency footprint, conventional entry point. | Reproducible dependency resolution and CI across declared Python versions. |
| `aeon.bat` | Starts in project directory and bootstraps local environment. | Detect incomplete environments/version drift; leave supervision to an external process manager. |
| `README.md`, `HOSTING.md`, `.gitignore` | Honest experiment framing, operational guidance. | Refresh counts/modes/journal names/market scope; qualify stop/restart guarantees; document recovery; ignore DB WAL/SHM sidecars. |

## 4. Prioritized correctness findings

Priorities are my engineering judgment: **P0 = release blocker for unattended live use; P1 = important reliability work; P2 = research/maintainability**. These are not predictions of financial loss.

### F1 — P0: Confirmed entries are not immediately reflected in account state

Sources: `aeon/okx_broker.py:166`, `aeon/okx_broker.py:203`, `aeon/agent.py:373`.

`OkxBroker.account()` refreshes balances but constructs positions from `_known`. Derivative `open()` returns a confirmed fill without inserting the position there; the monitor populates it later. Add/reduce state has related lag. A fresh account call therefore need not be a fresh position snapshot.

**Reproduced:** After a successful mocked derivative entry, `account()` reported an empty position dictionary before the next monitor tick.

**Impact:** Consecutive proposals can pass duplicate-position, position-count, exposure and risk checks against old positions. An immediately following adjustment can be rejected because the just-opened position is missing. The lock does not eliminate this window.

**Improvement:** Update canonical position/exposure reservations after every execution and reconcile before the next action. Do not declare a snapshot current solely because balances were refreshed.

### F2 — P0: Spot balances are being used as ownership evidence

Sources: `aeon/okx_broker.py:112`, `aeon/okx_broker.py:459`, `aeon/okx_broker.py:482`.

Tracked spot quantity is intersected with total wallet balance. Suppose the account held 10 SOL before AEON bought about 2 SOL, then AEON's stop sells its 2 SOL. The remaining 10 SOL still satisfies the balance test. AEON can believe its 2 SOL position remains and re-arm a stop against pre-existing inventory.

The independent broker review reproduced this with a fake exchange: no closing fill was emitted and a replacement stop was placed against the old tracked quantity. Partial external sales also leave owned quantities and protective coverage inconsistent.

**Improvement:** Derive owned inventory from attributed executions and cumulative fills. Use balances only to constrain sellable inventory. A dedicated account reduces interference but is not a substitute for correct ownership accounting.

### F3 — P0: Unknown and partially filled orders lack durable recovery

Sources: `aeon/okx_broker.py:182`, `aeon/okx_broker.py:203`, `aeon/okx_broker.py:280`, `aeon/okx.py:168`.

`_await_fill()` abandons confirmation after eight polls and raises for canceled orders without accounting for partial quantity. Desired protection and spot ownership are generally recorded only after confirmation. An accepted order whose response is lost is another ambiguous state.

Avoiding POST retries is good, but does not prove an order did not execute. Generated client IDs are not currently a persisted recovery workflow.

**Improvement:** Persist intent/client ID before submission. Represent pending, partially filled, filled, canceled-with-fill and unknown outcomes. Reconcile by stable IDs before a conflicting action. Recovery must not depend on another LLM decision.

### F4 — P0: Protection is not verified as covering the intended position

Sources: `aeon/okx_broker.py:155`, `aeon/okx_broker.py:421`, `aeon/okx_broker.py:482`.

The protective-order view collapses each instrument to trigger prices and IDs. Any same-instrument stop can satisfy monitoring without checking ownership, direction, position side or covered quantity. A small old stop can look like complete protection.

Replacement cancels discovered same-instrument algos before placing new protection. Failure between those operations creates a gap; failed cancellation can leave overlapping orders. Cancellations are not scoped to AEON-owned protection.

The one-minute fail-safe is conditional: initial read failures bypass protection evaluation; its timer is not persisted; placement success clears it without subsequent observed coverage.

**Improvement:** Explicitly verify coverage and ownership. Preserve old protection until amendment/verified replacement where the API permits it. Treat unknown protection as degraded, persist its deadline and expose failed emergency actions. Verify API-specific replacement semantics separately.

### F5 — P0: A completed trade can be surfaced as a failed operation

Sources: `aeon/okx_broker.py:223`, `aeon/okx_broker.py:238`, `aeon/okx_broker.py:302`, `aeon/okx_broker.py:323`.

Several add/reduce paths confirm execution, then await protection updates before returning `Fill`. If protection fails, the method raises and the caller never records the completed execution.

**Improvement:** Separate execution and protection outcomes. Always persist/surface confirmed fills; attach protection failure as a distinct recovery state. An operation error must not imply exposure was unchanged.

### F6 — P0: Hedge-mode identity loses one side

Sources: `aeon/okx_broker.py:144`, `aeon/okx_broker.py:155`, `aeon/broker.py:89`.

The broker has hedge-mode payload handling, but positions/protections are keyed only by instrument. Opposite-side rows overwrite one another. The independent review reproduced loss of one side.

**Improvement:** Either explicitly reject hedge-mode operation in this version or key positions by instrument, side and relevant account/margin scope throughout. Supporting an API field is not equivalent to supporting its position model.

### F7 — P0: Non-finite numbers bypass intended risk checks

Sources: `aeon/brain.py:229`, `aeon/brain.py:255`, `aeon/risk.py:204`.

Parsing accepts floating-point NaN/infinity; ordinary comparisons do not reliably reject them. The JSON extraction path does not reject non-standard numeric constants either.

**Reproduced:** A NaN-stop open proposal received `Verdict(ok=True, contracts=0.4, notional=40.0, max_loss=nan, stop_loss=nan)` for a simple $200 account. This establishes an accepted invalid verdict, not a claim that the exchange would accept it.

**Improvement:** Finite-value and range validation at every boundary: model schema, configuration, data, account and final broker command. Reject malformed output rather than permissively coercing it. Bound action count and incompatible batches.

### F8 — P1: Close acknowledgment retires protection before flatness is established

Sources: `aeon/okx_broker.py:259`, `aeon/okx_broker.py:398`.

After the close endpoint returns, local known/desired state is deleted and protection canceled without a fresh flat-position check. If the response is only acknowledgment or execution remains incomplete, exposure can be rediscovered without its intended protection.

**Improvement:** Keep a closing state until fills/reconciliation establish zero remainder. Code ordering is verified; the failure scenario depends on the exchange response contract and needs authorized integration verification.

### F9 — P1: Freshness is global, incomplete and checked too early

Sources: `aeon/feed.py:28`, `aeon/feed.py:67`, `aeon/feed.py:80`, `aeon/agent.py:317`, `aeon/agent.py:373`.

A tick for any instrument refreshes one timestamp. REST fetches only SWAP tickers despite spot/futures support. A fresh perpetual can hide stale prices for another position. Cached prices have no per-instrument age.

The cycle checks freshness before snapshot construction/model latency. Actions use cached prices selected before acquiring the broker lock, without a final age/deviation limit.

**Reproduced:** With spot and swap subscriptions, REST requested only `SWAP`, updated only the swap, and still made global feed age appear fresh.

**Improvement:** Timestamp each instrument/snapshot. Immediately before new risk, validate its own price age, account generation and permitted price deviation. Block new risk on missing data while continuing feasible recovery/risk reduction.

### F10 — P1: Persistence boundaries and cycle links are inconsistent

Sources: `aeon/agent.py:244`, `aeon/agent.py:350`, `aeon/agent.py:456`, `aeon/journal.py:33`, `aeon/journal.py:61`.

Broker state is saved during monitor ticks, not atomically with every execution/trade/risk record. Crash, cancellation or disk failure between writes can leave execution, trade row, risk counter and restored position inconsistent.

`Journal.add_cycle()` returns a database ID, but `_book_decision()` discards it and actions use the process-local cycle number. That counter restarts at zero, potentially linking new-session actions to old cycles. Rejections use a null cycle ID.

**Improvement:** Use inserted database IDs for all outcomes, coordinate transactional checkpoints, and reconcile executions idempotently on restart. Persist decision inputs as well as outputs.

### F11 — P1: Paper spot buying power includes unrealized gains

Sources: `aeon/broker.py:132`, `aeon/broker.py:143`, `tests/test_paper_kinds.py:18`.

Availability is marked equity minus entry-cost margin. With $200, buying $100 spot leaves $100 cash. If it doubles, paper mode reports $200 available and accepts a further $150 purchase without selling. The independent review reproduced this; an existing test expects the inflated availability.

**Improvement:** Separate cash, inventory market value and derivative collateral. Correct the test rather than preserve behavior merely because tests pass. Align paper/live buying power before trusting comparisons.

### F12 — P1: Realized accounting and synthetic capital require reconciliation

Sources: `aeon/okx_broker.py:364`, `aeon/okx_broker.py:398`, `aeon/journal.py:88`, `aeon/agent.py:69`.

Spot closes are reconstructed from timestamp-matched sells; derivative history selects a recent row without full lifecycle identity. Estimates can become permanent. Derivative closes assign one history value to both incremental cash P&L and whole-trade P&L despite earlier entry fees/reductions being booked. Exact duplication depends on the exchange field contract and needs verification.

The capital budget uses configured capital plus cached journal realized results and current unrealized P&L. It inherits missing/duplicated/estimated records and can lag fresh fills. Funding and independent account cash flows do not form a reconciled strategy ledger.

**Improvement:** Attribute fills/charges by ID, distinguish cash deltas from lifecycle totals, paginate recovery, correct provisional estimates and derive capital from the canonical ledger. A budget is a sizing overlay, not account isolation.

### F13 — P1: Validate the final executable command

Sources: `aeon/risk.py:204`, `aeon/risk.py:274`, `aeon/agent.py:373`, `aeon/agent.py:449`.

Stops are evaluated before tick rounding, which can change distance, side validity and tightness. Sizing uses a reference price rather than the fill and includes assumed fees but not a full gap/slippage/funding model.

**Improvement:** Quantize deliberately, revalidate the complete command, and recompute modeled risk. Refresh daily/drawdown status from current equity before each new-risk action. Add aggregate stop-risk and correlated-exposure limits. Three coins are not automatically independent risks. Describe per-trade loss limits as modeled stop budgets, not guaranteed realized-loss ceilings.

### F14 — P1: Configuration and presentation are not strict domain boundaries

Sources: `aeon/config.py:105`, `aeon/config.py:149`, `aeon/tui.py:506`.

Validation omits many numeric bounds/finiteness/cross-field checks; numeric coercion can truncate fractional counts. The component review found negative budget, NaN risk percentage, zero monitor interval and inverted stop bounds accepted. Negative budget is especially misleading because only positive budgets activate the capital overlay.

UI rendering also assigns `p.mark` on account position objects outside the trading synchronization path. Copies of position dictionaries do not clone the mutable objects they contain.

**Improvement:** Reject invalid configuration before constructing runtime objects. Render immutable display snapshots and keep the TUI out of domain-state mutation.

### F15 — P1/P2: Notifications are useful but not a guaranteed emergency channel

Sources: `aeon/notify.py:250`, `aeon/notify.py:269`, `aeon/notify.py:276`, `aeon/notify.py:314`, `aeon/notify.py:360`.

Critical halts share the same admission cap/queue as routine events. They can be dropped or displaced under load (P1). Delivery exceptions only increment a counter. With cooldown zero, dedupe entries are still inserted without the corresponding pruning path, creating unbounded growth. Closing an unstarted worker can process its queue outside the normal flush deadline (P2).

**Improvement:** Reserved critical delivery capacity, structured severity/ping authority, local sanitized health warnings, bounded dedupe memory and bounded total shutdown. An external heartbeat/process monitor is still needed. Document best-effort delivery, including that `--check` now sends a Discord test when configured.

## 5. My opinion of the decision engine

### What I would keep

1. **The model proposes; code authorizes.** This is much better than giving an LLM direct exchange-tool authority.
2. **A real hold option.** No trade should be a successful result, not a lack of activity.
3. **The heuristic baseline.** It is a cheap testable alternative, not established evidence of a profitable strategy.
4. **Rationale and execution feedback.** Valuable when attached to durable inputs, outcomes and IDs.
5. **Independent monitoring while the model thinks.** Preserve that separation.

### What I do not yet trust

**The model has broad discretion without an empirical contract.** It chooses direction, size, leverage, stop, target and next-check time from indicators and recent outcomes. Nothing in the inspected implementation demonstrates that this outperforms the heuristic after costs or remains stable under small prompt changes.

- A plausible explanation is not a measured advantage.
- `confidence` is proposal metadata/logging, not a calibrated probability. I would not increase size because the model reports high confidence.
- Self-written notes are bounded memory, not a training update or validated learning algorithm. Recent wins/losses do not establish causal learning.
- Recent outcome context is small and selected from recent events before closed-trade filtering; it can contain less outcome evidence than its heading implies.
- The prompt lacks a complete executable-price/cost view: spread, depth, realized slippage, financing schedule and explicit data-quality flags are missing/incomplete.
- Missing RSI/ATR rendered as zero obscures unavailable data versus a genuine extreme value. Static funding labels and mixed current/closed-bar features overstate precision.
- Model-selected next-check timing should never control the independent protection/reconciliation heartbeat. The existing split is good.

### The architecture I would choose

```text
Validated timestamped observations
  -> explicit candidate setups / no-setup reasons
  -> bounded model ranking, critique or selection
  -> typed trade intent with thesis, invalidation and expiry
  -> deterministic portfolio allocator and governor
  -> final execution validation
  -> durable execution/protection state machine
  -> attributable outcomes and evaluation
```

Start with the model selecting or abstaining among a few versioned setup families, explaining its choice and suggesting a bounded horizon. Deterministic code owns sizing, leverage eligibility, feasibility, portfolio limits and recovery. Greater autonomy should be earned through experiments.

Record strategy/setup ID, instrument/side, observation IDs, horizon, entry conditions, invalidation, exit policy, costs and decision expiry. Do not demand fabricated numerical win probabilities. Use structured position memory: entry thesis, invalidation, management plan and actual execution state. Free-form notes remain commentary, not account truth. Model/prompt changes create new experiment versions.

## 6. How I would establish whether the engine helps

The important missing feature is **reproducible evaluation**, not a more elaborate prompt.

1. **Capture complete inputs:** timestamped universe/features, prices and ages, account, prompt, configuration/model versions, reply, parsed proposals, verdicts and fills. Raw-response logging is insufficient to reconstruct decisions.
2. **Deterministic replay:** replay observations through the same governor/simulator without live data or new model calls. Preserve original outputs when evaluating execution changes.
3. **Compare on identical observations:** no-trade, heuristic, LLM selection and hybrid. No-trade measures avoided turnover/cost; it is not automatically the appropriate investment benchmark for every objective.
4. **Chronological out-of-sample evaluation:** choose parameters on earlier data and evaluate on later unseen windows. Reconstruct the historical universe, including expired/delisted instruments. Do not choose the winning prompt using the final evaluation set.
5. **Realistic costs and stress:** instrument-specific fees, spread, slippage, funding, partial fills, delays and downtime; include model-call cost and its uncertainty.
6. **More than win rate:** net P&L, drawdown, turnover, risk-unit outcomes, tail losses, time in market, individual cost components, reject rate, unprotected/unknown-order duration and recovery success.
7. **Shadow before promotion:** a candidate proposes while the execution policy stays unchanged. Require written operational/evaluation criteria, not a short winning streak.

Market features need their own specification. `aeon/market.py:91` sorts candles but ignores confirmation, duplicates, gaps and freshness. `Market._one()` mixes ticker prices and candle closes. The labels 15m/1h/4h should correspond to time-aligned observations, not approximate array offsets. Require sufficient warm-up for EMA50 rather than accepting only 20 candles for every feature. Isolate malformed-market failures instead of letting one parser error fail the complete snapshot.

## 7. What I would build, in order

### Phase A — Make account state trustworthy

- Durable order intent, execution IDs and unknown-outcome recovery.
- Immediate position/exposure updates, startup reconciliation and canonical ledger.
- Correct spot ownership, hedge-mode policy and spendable-cash accounting.
- Verified protection coverage and safe replacement/closing lifecycle.
- Finite-value/schema checks and final quantized-command validation.

**Exit criterion:** Fault-injection tests show no silently lost fills, duplicate reconciliation, unknown exposure treated as flat, or unrelated inventory treated as owned.

### Phase B — Make operations observable and recoverable

- Explicit running, paused, degraded, halting, halted and recovering states; invalidate in-flight decisions when the control generation changes.
- Per-market freshness, supervised tasks, critical alerts and an external heartbeat.
- Headless pause/kill/resume/status using a local or authenticated boundary, never an unauthenticated public trading-control endpoint.
- Durable checkpoints, schema migrations, backups, single-instance/account ownership checks.
- Runbooks for unknown order, missing stop, failed flatten, stale feed, network loss, restart and database failure.

**Exit criterion:** An operator can distinguish protected from unknown, identify a stalled monitor, inspect pending recovery and halt/resume without editing database rows.

### Phase C — Prove or reject decision value

- Snapshot archive, deterministic replay, realistic simulator and versioned experiments.
- Heuristic/LLM/hybrid comparisons on common observations and chronological holdouts.
- Structured intents/memory, explicit no-trade criteria and bounded decision expiry.
- Only then additional markets, features, models or adaptive policies.

**Exit criterion:** The policy meets written out-of-sample and operational requirements. No profitability target or universal sample count can be certified by this review.

## 8. Tests I would prioritize

| Failure or invariant | Expected assertion |
|---|---|
| Entry then another action before monitor tick | Exposure/position limits include the first fill. |
| Lost response after accepted order | Recover by identity; no blind second entry. |
| Partial fill followed by cancellation | Acquired quantity/fees recorded and protected. |
| Confirmed add, failed protection | Fill remains booked; protection state becomes degraded. |
| Pre-existing spot plus AEON stop-out | Retire owned inventory; do not re-arm against old coins. |
| Opposite hedge sides | Represent both, or explicitly refuse unsupported mode. |
| Foreign, wrong-side or undersized stop | Does not satisfy protection invariant. |
| Cancel succeeds, replacement fails | Recovery preserves/restores known protection. |
| Close acknowledged, remainder exists | Keep closing/recovery/protection state. |
| One live swap, stale spot/future | Stale instrument cannot inherit global freshness. |
| NaN/infinity/invalid schema or configuration | Reject before broker commands. |
| Coarse tick/lot sizes | Final size/prices/tightness/modeled risk remain valid. |
| Crash at each persistence boundary | Restart produces one record per execution and correct state. |
| Restart after multiple cycles | Actions link to the actual database cycle ID. |
| Spot appreciates without a sale | Spendable cash does not increase. |
| Entry, partial exits, funding, final close | Cash deltas reconcile independently of lifecycle P&L. |
| Repeated monitor/API failures | Block new risk, expose faults and supervise recovery. |
| Alert flood followed by kill | Critical event retains delivery capacity. |
| Dedupe disabled or notifier shutdown | Memory and total drain time remain bounded. |
| UI refresh | Domain position/account objects are unchanged. |

Use an injectable clock and deterministic exchange fakes for most tests. Maintain a separately authorized integration suite for API contracts; normal unit tests must never place real orders through ambient credentials.

## 9. Documentation and operational consistency

The root configuration selected demo at inspection while the README/launcher described paper as default. Automatic journal names are mode-specific in code, while some prose still says `aeon.db`. Older review tables/test counts and perpetual-only descriptions in hosting guidance no longer match the expanded market surface. Update these rather than preserving outdated guarantees.

Qualify claims that every entry is protected immediately: the spot path buys first and submits protection afterward. Likewise, one-minute repair depends on working API reads/writes; persistence is periodic, not a zero-loss crash guarantee. Diagnostics and best-effort alerts are useful, not runtime permission or supervision guarantees.

The existing `.gitignore` excludes `*.db` but not SQLite WAL/SHM sidecars. Keep secrets and account artifacts out of source control. The directory was not a Git repository during review; do not mistake its presence of `.gitignore` for tracked history or a reproducible release.

## 10. What I would deliberately not do now

- **No Rust rewrite:** correctness/network/model boundaries are the immediate issues.
- **No second critic model yet:** added cost and failure paths before proving the first model helps.
- **No sizing from uncalibrated model confidence.**
- **No news/social ingestion without provenance and untrusted-text boundaries:** it adds a prompt-injection/data-quality surface.
- **No wider autonomous scope to compensate for weak results:** more actions/markets make attribution harder before measurement exists.
- **No microservices or queue for appearance:** a modular single process with a durable lifecycle can address current needs.

## 11. Bottom line

**Keep AEON, but strengthen its truth model before its intelligence.** The governor is the right architectural choice, but its guarantees depend on accurate state, valid inputs, executable prices and verified outcomes. Those dependencies have concrete gaps.

The next version need not trade more. It should know exactly what it owns, what executed, which risk is protected, why each decision was made, and whether the model improves outcomes after realistic costs. That turns a compelling prototype into an experiment whose conclusions can be trusted.

## 12. Final validation checkpoint

At **12:46 Europe/Paris on October 17, 2026**, after the concurrent Discord changes, the offline suite completed with **102 passed in 1.03 seconds** using:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

This command disables bytecode and pytest cache writes. Tests used fakes/mocks; no live account/model session was started. No application or test implementation was edited. The report records proposed changes only.

Independent in-memory probes reproduced spot ownership confusion, hedge-side overwrite and paper cash inflation. Main-review probes reproduced accepted NaN risk, missing immediate post-entry position state and SWAP-only fallback with apparently fresh global age. These probes were not added as project tests.

Core reviewed-source SHA-256 fingerprints, unchanged between the 12:39 and final checkpoint:

| File | SHA-256 |
|---|---|
| `aeon/brain.py` | `418DEC34D1DA8A44495C67A4842A688859F1CEBB2C603E54970F60895787CBA5` |
| `aeon/risk.py` | `C0669A6417D900925989B2A43B7495917CE4CEA4CF0A4C62E9FB71A7F60B1F98` |
| `aeon/okx_broker.py` | `F2493345488DF011388B57B779E872DAD8A8613A7586914FBBA99BCCD8A63B6C` |
| `aeon/feed.py` | `38EC541B8A660246C64F3CDADE10550CF120D7D5550847E2278BBF7D5C6C55A6` |

The test result is a validation checkpoint, not a safety certification. In particular, mocks that return normal successful payloads do not establish behavior under partial fills, uncertain writes, replacement failure or process interruption.

## 13. Status after the first fix round (October 17, 2026)

The project was put under git first (baseline commit `10d27ec`). Then the critical and high findings were fixed and each reproduced failure was turned into a regression test (`tests/test_okx_broker_kinds.py`, `tests/test_go_live_fixes.py`). Offline suite: **151 passed**. The reproduction probes for F1, F2, F5, F7 and the spot buy-recovery case now produce the correct outcome. **Still not verified against a real OKX account**: that is the purpose of the demo phase.

| Finding | Status |
|---|---|
| F1 post-entry state | **Fixed.** Every booked fill updates positions immediately (provisional until OKX's next report). |
| F2 spot ownership | **Fixed.** Ownership comes from AEON's own fills and its own stop orders' reported executions (`algo_details` → `ordIdList`); balances only cap what may be sold. Coins leaving without an AEON order are booked as "sold outside AEON". Spot cancellations touch only AEON's own stops. |
| F3 unknown / partial orders | **Fixed for market orders** (open, add, reduce, spot sell): intent and client ID are saved before sending; lost replies and unconfirmed fills become `PendingOrder` and are reconciled by client ID; canceled-with-partial-fill is booked. **Open:** the derivative `close-position` path has no client-ID recovery. |
| F4 protection verification | **Partly.** Spot: only AEON's own stop counts as protection, and re-arming waits (up to 60s) when a stop left the book until OKX says why. **Open** for perps/futures: covered size and side are not verified; cancel-then-place still has a gap. |
| F5 fill surfaced as failure | **Fixed.** Executions are always returned and journaled; a failed stop replacement is reported separately as UNPROTECTED and retried. |
| F6 hedge mode | **Open** (the account uses net mode). |
| F7 non-finite numbers | **Fixed** at the JSON parser, the risk engine and both brokers. |
| F8 close acknowledgment | **Open.** |
| F9 freshness | **Fixed.** Per-instrument price timestamps, REST fallback for all three kinds, re-fetch before acting on a price older than 60s, no new risk on a price older than 5 minutes. |
| F10 persistence / cycle IDs | **Partly.** Broker state is saved the moment an order is recorded or booked. Cycle-ID linking is still open. |
| F11 paper spot buying power | **Open.** |
| F12 accounting reconciliation | **Open** (derivative partial reductions are still estimates). |
| F13 final command validation | **Open.** |
| F14 configuration validation | **Open.** |
| F15 notifications | **Fixed.** Halt and unprotected-position alerts bypass the burst cap and go out ahead of routine ones (in order); dedupe memory stays bounded when the cooldown is 0. An external heartbeat is still needed. |

## 14. Status after the production-readiness round (October 19, 2026)

Scope: deploy to a VPS for unattended running. Offline suite: **219 passed**. Same caveat as before: nothing here has
touched a real OKX account. A read-only look at the demo journal on the dev machine showed 4 decision cycles in 25 hours
and no trades, so the order, stop and reconciliation code is still verified against fakes only. `aeon --smoke-demo` and a
demo soak on the VPS (see `DOCKER.md`, gates 2 and 3) are the way to change that.

| Finding | Status |
|---|---|
| F3 lost / ambiguous write | **Improved.** An HTTP 5xx, or an unreadable 2xx reply, on any write is now reported as *outcome unknown* (`net`) instead of a definite rejection, so the order is looked up by client id instead of being forgotten. The derivative `close-position` path still has no client-id recovery. |
| F6 hedge mode | **Closed by refusing.** AEON stops at start-up on a long/short position-mode account with a message saying how to switch to net mode. Hedge support itself is not built. |
| F10 cycle linkage | **Fixed.** Every action (including rejections) is linked to the cycle's database id, not the per-run counter that restarts at 0. Persistence is still not one transaction per execution. |
| F14 configuration | **Fixed.** NaN/inf, out-of-range percentages, zero or negative limits, inverted stop bounds, fractional counts, a non-list where a list is expected, a zero monitor interval and a non-https LLM URL are all rejected at start-up with the setting named. |
| F15 external heartbeat | **Added, optional.** `HEARTBEAT_URL` pings a dead man's switch service while the monitor is healthy. |
| F4, F8, F11, F12, F13 | **Unchanged / open.** Perpetual stop coverage (size, side) is not verified and replacement is cancel-then-place; a close that is acknowledged but not complete is not re-checked; paper spot buying power and derivative partial-reduction P&L are still wrong or estimated; the final rounded command is not re-validated (the exchange rejects a bad trigger price, so this fails closed). Not changed because the right behaviour depends on OKX responses that cannot be tested without an account. |

Operations added: a single-instance lock on the journal; a heartbeat file, `--healthcheck` and a watchdog that exits a
wedged agent (Docker restarts a container that exits, never one that is merely unhealthy); SIGTERM handled like Ctrl-C;
a clear one-line failure and non-zero exit when start-up fails; a clock-skew line in `--check`; `--smoke-demo`; a
hardened compose file (read-only filesystem, no capabilities, memory and pid caps); pinned dependencies
(`requirements.lock`); an online, integrity-checked backup script; and runbooks in `DOCKER.md`.

Found while doing this, not fixed: a spot entry raised to the exchange minimum order size can end below the minimum
*sell* size once the buy fee is taken in the coin, so it cannot be sold and is dropped from tracking (the coins stay in the
wallet). It needs a spot pair whose minimum order is close to the intended trade size, which the affordability filter makes
unlikely at 200 USDT.
