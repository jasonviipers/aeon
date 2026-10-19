# AEON: an autonomous trader for OKX perpetuals, expiry futures and spot, in your terminal

An LLM agent that watches OKX's perpetual swaps, dated expiry futures and spot markets, decides for itself
what to trade, in which market, how big, with what leverage and where the stop goes, manages its positions
around the clock, and shows everything it thinks and does in a Bloomberg-style terminal cockpit.

```
 ◢◤ AEON  AUTONOMOUS PERPETUALS TERMINAL     PAPER  ● RUNNING  cycle #12 · next 04:20     feed WS ● 0s   09:46 UTC
 ETH 2,433 ▼0.7%  BTC 81,638 ▼0.5%  SOL 104.89 ▼1.4%  NEAR 5.145 ▼2.4%  XRP 1.305 ▼2.5% ...
 ┌ EQUITY ┐┌ TOTAL P&L ┐┌ TODAY ┐┌ DRAWDOWN ┐┌ WIN RATE ┐┌ BRAIN ┐
 ┌ WATCHLIST · LIVE ┐ ┌ EQUITY CURVE ─────────┐ ┌ RISK GOVERNOR ┐
 │ ticks, 24h, RSI… │ │ ▁▂▃▅▆█                │ │ bars vs limits │
 │                  │ ├ AGENT STREAM ─────────┤ ├ EXECUTIONS ────┤
 │                  │ │ what it thinks & does │ │ fills + P&L    │
 ┌ POSITIONS ─────────────────────────────────────────────────────────────────────────────────────┐
  p Pause   c Think now   m Memory   f Flatten   k Kill switch   r Resume   q Quit
```

## Read this first

* **No one can promise this makes money, and the most likely outcome of a small, leveraged,
  LLM-driven futures account is that it loses.** Fees alone are ~0.1% per round trip. Treat the
  200 EUR as money you can afford to lose entirely, and treat AEON as an experiment you are measuring.
* **Run it in this order, and only move on when each stage has behaved for a while:**
  `paper` (simulated, no keys) → `demo` (OKX demo-trading account, fake money, real order flow) → `live`.
  What is verified: the scanner (against live OKX data for all three markets), risk engine, paper broker,
  LLM brain (including a real DeepSeek run), agent loop and UI (219 automated tests). What is **not**
  verified: the signed OKX trading calls (`okx_broker.py`) against a real account. They follow OKX's v5
  docs and are tested against a fake exchange that mimics OKX's order mechanics, but `demo` mode is where
  you confirm they behave on your account: `aeon.bat --smoke-demo` places, protects, moves and closes the
  smallest possible orders on OKX demo and reports pass/fail, so run it first. That matters most for **spot** (its stop order is placed right
  after the buy, and fees are taken in the coin) and for the **dated futures**, which are new.
* **LLM cost is a real drag on 200 EUR, so it is measured.** A cycle is ~5,200 tokens in / ~450 out (all three
  markets). At 96 cycles a day a frontier model can cost about $2/day (around 1% of the account, daily);
  `deepseek-flash` measured $0.0021 a cycle. The agent chooses its own next check-in (5-60 min), so it thinks
  less when nothing is happening. The **LLM METER** panel in the bottom-right corner shows tokens in, tokens
  out, total cost, the rolling last 24 hours, and the **drag** (24h cost as a share of your equity per day).
  Cost needs your model's per-million-token prices in `config.toml` (`input_price_per_m`, `output_price_per_m`);
  without them the panel says "no prices set". Each cycle also logs its tokens and cost in the agent stream.

## Quick start (Windows)

```bat
cd aeon
python -m venv .venv
.venv\Scripts\python -m pip install -e .
copy .env.example .env          :: then edit .env (the keys you need depend on the mode)

aeon.bat --check                :: verifies config, keys, connectivity
aeon.bat --brain heuristic      :: free dry run: rule-based bot, no LLM key, paper money
aeon.bat                        :: paper mode with the LLM agent (needs LLM_API_KEY)
aeon.bat --headless             :: no UI, logs to the console: use this on a server
```

Use **Windows Terminal** (not the legacy console) so the block characters and colours render properly.

`aeon.bat` is the recommended way to start AEON: it always uses the project's own virtual environment. Plain
`python -m aeon ...` works too: if your system Python lacks AEON's packages (`httpx`, `textual`, `websockets`),
it notices, says so, and re-runs itself with the project's `.venv`. If there is no `.venv` it tells you to run
`aeon.bat` or `python -m pip install -e .` instead of printing an ImportError.

## What the agent does each cycle

1. Scans **every** market it may trade (about 290 perpetuals, the dated futures and about 300 spot pairs;
   stock and commodity contracts are opt-in in `config.toml`), keeps the most liquid few of each kind plus
   anything you hold, and drops contracts whose minimum order can't fit your risk limits.
2. Builds a compact snapshot: account, positions with stops, per-market price/trend/RSI/ATR, funding
   (perps) or basis and days to expiry (futures), 24 hourly closes, its own recent actions and their
   outcomes, and **its own notes from last cycle** (its memory).
3. Asks the model for one JSON decision: `open`, `add`, `reduce`, `close`, `adjust` (trail the stop, move
   the target), or `hold`, with a reason for each, new notes for itself, and **when to look again**.
4. Passes every action through the risk governor, executes what survives, and logs everything to a
   SQLite journal (`aeon-paper.db`, `aeon-demo.db` or `aeon-live.db`, one per mode so they never mix):
   the equity curve, every decision, the raw model reply, tokens and cost.

Between cycles a monitor loop re-checks prices (WebSocket, falling back to REST), stops, equity and the
kill switches every few seconds.

## The markets

| Market | What it is | What AEON does with it |
|---|---|---|
| **Perpetual** (`SOL-USDT-SWAP`) | USDT-margined swap, no expiry, pays funding | long or short, 1-5x, isolated margin |
| **Dated future** (`BTC-USD_UM-261030`) | USDⓈ-margined futures that **expire** (this month, next month, quarter). **Off by default**: add `"futures"` to `universe.markets` once you have watched a demo day without it | same as a perpetual, plus: never opened within 3 days of expiry, **closed 1 day before it**, basis and annualised basis shown to the agent |
| **Spot** (`SOL-USDT`) | the coin itself | **long-only**, no leverage, never liquidated. 0.1% fee per side. AEON only ever sells coins it bought itself, so anything you already hold is safe |
| Margin, options, events | | not built (see below) |

* **One position per coin** across all three (a SOL spot position blocks a SOL perpetual), so the agent can't
  accidentally double up or hedge itself into fees.
* **Dated futures are thin.** BTC's nearest trades roughly $1M a day against about $5B for its perpetual, so
  they get their own lower volume bar and one expiry per coin. OKX also lists 244 "XPERP" futures that expire
  in 2031 and behave like perpetuals; set `futures_include_xperp = true` to add them.
* **Spot stops:** a spot buy cannot carry an attached stop (the fee is taken in the coin you buy), so the
  stop order is placed straight after the buy and the monitor re-arms it if it is ever missing.
* **A market your OKX account mode can't trade is switched off automatically**, with a warning. In OKX's *Spot
  mode* AEON trades spot only; switch the account to *Spot and futures mode* and restart to enable the rest.
* **Margin trading and options** are not built: margin adds borrowing interest on top of what perpetuals
  already give (shorting, leverage), and options and OKX's event contracts have a different risk shape
  (premium decay, all-or-nothing payoffs) that deserves its own design.

## The agent is free to decide; the code is not negotiable

The model chooses *what* to do. These limits are enforced **in code after it decides**, so it cannot
argue past them (all in `config.toml`, rejected or clipped actions are shown to the model next cycle so it learns). The shipped
values are deliberately conservative for a ~200 USDT account that has never traded for real; see the comments in
`config.toml` for what they work out to and for the next step up:

| Rule | Default |
|---|---|
| Max leverage | 2x |
| Max loss if stopped out (size is clipped to fit) | 0.5% of equity |
| Stop required on every position, distance | 1%-8% from price |
| Stops may only move closer, never further away | on |
| Margin per position / open positions / total exposure | 30% / 2 / 1.5x equity |
| Daily loss limit (then no new positions until 00:00 UTC) | 2% |
| Drawdown from peak: **flatten everything and halt until you press R** (or `--ctl resume --yes`) | 10% |
| Re-entry cooldown on a contract / max entries per day | 60 min / 10 |
| Markets traded | perpetuals and spot (dated futures are opt-in) |
| One position per coin across spot / perp / future | on |
| Dated futures: no new position within / force-close before expiry | 3 days / 1 day |
| Spot | long-only, 1x |
| Margin mode | isolated only |
| Capital cap on demo/live (the rest of the account is never touched) | `budget_usdt = 200` |

On real accounts (`demo`/`live`), perpetual and futures entries are market orders with their stop-loss and
take-profit **attached and held on OKX's servers**, so protection survives AEON crashing or your network
dropping. A spot buy can't carry an attached stop, so its stop is placed straight after the fill. AEON
re-checks every few seconds that each position still has a stop and re-arms it if not; if it can't restore a
stop within a minute it closes the position.

How AEON keeps its books straight on a real account:

* **Every order is recorded and saved before it is sent**, under an ID AEON chooses. If OKX's reply is lost or
  the fill can't be confirmed in time, the agent reports the order as *pending* (not failed), and the next
  monitor pass looks it up by that ID and books what really happened. If OKX never received it, it's
  dropped after a minute.
* **Spot holdings are AEON's own fills, never "whatever the wallet holds".** Coins you already own are never
  counted, protected or sold, and stops you placed by hand on them are never cancelled. When AEON's own stop
  or target fires, it learns that from the stop order itself, so the sale is booked even if you hold more of
  the same coin. Coins that disappear without an AEON order are booked as "sold outside AEON".
* **Every fill updates positions immediately**, so a second decision in the same cycle sees the first.
* **A trade that executed is always booked**, even if replacing its stop then fails: the failure is reported
  separately as *UNPROTECTED* and retried.
* **No new risk on a stale price.** Each instrument's price has its own age; older than 60s it is re-fetched
  from OKX before acting, and if it's still older than 5 minutes AEON won't open or add (closing is always allowed).
* **Non-numbers never reach an order.** A `NaN`/`Infinity` from the model is treated as missing and rejected.
* **Halt and unprotected-position alerts skip the Discord rate limit** and go out ahead of routine messages.

## Going from paper to real money

1. **Paper** (default): needs no OKX keys. Simulated fills on live prices with fees and slippage.
   Funding payments are not modelled. State survives restarts.
2. **Demo**: in OKX open *Demo Trading*, create demo API keys, put them in `.env`, set `mode = "demo"`.
   Run `aeon.bat --check`, then `aeon.bat --smoke-demo` (smallest orders on the demo exchange: buy, stop and
   target visible, stop moved, close, flat; the daemon must be stopped). Then watch that entries, stops, partial
   closes and stop-outs all behave, for **each market kind**. A demo account starts with ~100,000 USDT plus coins; `budget_usdt` makes AEON behave as
   if it had 200, so the sizes and fees look like the real thing.
3. **Live**, when you are satisfied:
   * Create a **separate OKX sub-account** for AEON and fund it with just the 200 EUR (as USDT). AEON
     manages every futures/perpetual position it can see (spot it only sells what it bought), so keep your
     other positions out of that account.
   * Set the account mode to *Spot and futures mode* (or Multi-currency margin). Spot mode can't trade
     perpetuals; `--check` tells you which mode you are in. The same applies to the demo account.
   * Create an API key for that sub-account with **Trade permission only. Never enable Withdraw.**
     Restrict it to your machine's IP.
   * Set `AEON_MODE=live` (or `mode = "live"` in `config.toml`) and `AEON_CONFIRM_LIVE=I-ACCEPT-REAL-MONEY-RISK` in
     `.env`. Without that phrase live mode refuses to start.

Never paste your API keys into chat, a ticket or a commit. `.env` is git-ignored.

## Running 24/7

Close the laptop and AEON stops thinking (exchange-side stops keep protecting open positions in
`demo`/`live`; paper positions freeze). For true 24/7 put it on a small always-on machine or VPS and run
`python -m aeon --headless`, under `systemd`, Task Scheduler or Docker. The UI and the agent currently run
in the same process, so a headless server has no cockpit; it logs to the console and to its journal database.

**Easiest path for a VPS:** a `Dockerfile` + `docker-compose.yml` are included — one container, a named
volume for the journal, `restart: unless-stopped`, secrets from `.env`, a read-only root filesystem, a healthcheck
 and a watchdog that restarts a wedged agent. **`DOCKER.md` is also the go-live checklist** (rollout gates, VPS
hardening, runbooks, backups):

```bash
cp .env.example .env && chmod 600 .env && nano .env   # keys + Discord webhook
docker compose build
docker compose run --rm aeon --check                  # verify before trading
docker compose up -d                                  # survives reboots and disconnects
```

## Controls

`P` pause/resume decisions · `C` think now · `M` read the agent's memory and trade theses ·
`F` flatten everything (confirm) · `K` kill switch: flatten and halt (confirm) · `R` resume after a halt ·
`Q` quit (confirm)

**The terminal UI fits the window you give it**, from about 60x18 up, and re-flows live when you resize:
150+ columns show three columns (watchlist, equity curve and agent stream, risk / executions / LLM meter);
100-149 show two columns with the risk row underneath; under 100 everything stacks in one column that scrolls
(mouse wheel, `↑` `↓` `PgUp` `PgDn` `Home`), with what the agent is doing right under the positions. Under 30 rows the
header and the six KPI tiles each become one line. Tables show as many columns as fit, most important first
(symbol, last, type, 24h... or symbol, side, P&L, mark, entry...), and the thesis column takes whatever width is left.

On a headless server (no terminal UI) the same actions are a local command against the running daemon:
`python -m aeon --ctl status|pause|unpause|cycle|flatten|kill|resume` (`--yes` is required for flatten, kill and resume).
It listens on `127.0.0.1` only and needs a token that only the AEON user can read; see `DOCKER.md` Section 5.

## Discord alerts

AEON can post what it does to a Discord channel as clean embeds: every fill (price, size, stop,
target, risk, PnL), each governor decision (rejections, adjustments, daily-loss limit), errors, and
one "AEON online" message each time it boots. Turn it on in `config.toml`:

```toml
[notify]
enabled = true              # needs DISCORD_WEBHOOK_URL in .env to actually send
events = ["fill", "risk", "error"]   # add "action" or "system" if you want more
ping_on_halt = true         # @here when the kill switch fires / AEON halts
```

Create a webhook with **Channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook
URL**, put it in `.env` as `DISCORD_WEBHOOK_URL`, then run `python -m aeon --check` to post a test
message. Ordinary messages never mention anyone, so model-written text can never ping your server; only
a halt does. Identical messages inside `cooldown_s` and anything above `max_per_minute` are dropped, so
a flapping exchange cannot spam the channel. A send failure is counted and swallowed — it never
interrupts trading.

## Layout

```
aeon/
  config.toml        all behaviour + risk limits          .env     secrets (git-ignored)
  Dockerfile         run it 24/7 on a VPS (see DOCKER.md)  docker-compose.yml
  aeon/
    okx.py           async OKX v5 client (public + signed)
    market.py        universe scan across perps / futures / spot, specs, indicators, basis
    feed.py          live prices over WebSocket (REST fallback)
    brain.py         prompt, LLM client, JSON parsing, rule-based baseline brain
    risk.py          the governor: sizing, stop rules, kill switches
    broker.py        paper broker          okx_broker.py   demo/live broker
    agent.py         24/7 loop: monitor + decision cycles + execution
    journal.py       SQLite journal         tui.py   the terminal UI
    notify.py        Discord alerts (webhook embeds)
    health.py        heartbeat file, Docker healthcheck, watchdog, optional dead man's switch ping
    smoke.py         `--smoke-demo`: exchange smoke test on OKX demo
  requirements.lock  exact dependency versions for the image    scripts/backup.sh   online journal backup
  tests/             219 tests (risk, paper accounting, order payloads and recovery against a fake OKX
                     exchange, market scanner, prompt/parsing, agent, alerts, UI helpers)
```

## Why Python, and the Rust question

The agent's "real time" is dominated by network and model latency (OKX ~100-300 ms, an LLM call 3-30 s),
and the parts that must react instantly (stops) live on OKX's servers, so Python is fast enough and far
quicker to iterate on while you tune the prompt and limits. The code is split into small modules
(`feed`, `broker`, `risk`) so the hot path can be ported to Rust later if you ever need
sub-second reactions, such as market-making or scalping, which is not what an LLM-driven agent does.

## Ideas for later

Backtesting the rule baseline against history, a second "critic" model that reviews each trade before
execution, drawing positions on the chart, and a read-only web view.
