# AEON — codebase review + running it 24/7 in the cloud

*Written after a full read of the source (`aeon/*.py`, ~130 KB), a run of the test suite
(35 tests at the time; **219 pass now**), and a review of `config.toml`, `.env.example` and the entry points.*

---

## 1. Short answer

**Yes — you can host it in the cloud, and the code is already built for it.**

* `python -m aeon --headless` exists precisely for this: no UI, every event printed to stdout,
  everything else identical to the desktop version.
* It only makes **outbound** connections (OKX REST + WebSocket, LLM API). It never listens on a
  port, so you don't need to open any inbound firewall rules — that's a security win too.
* All state that matters lives in SQLite (`aeon.db`): risk state, kill-switch flag, drawdown peak,
  paper account, the agent's memory/notes, token spend. Close the laptop, reboot the box, crash the
  process — it picks up where it left off. `systemd`/Docker restarts bring it back automatically.
* Dependencies are tiny (`textual`, `httpx`, `websockets`), Python ≥ 3.11. A $4–6/month VPS is
  more than enough.

What was **not** ready (section 5) was that headless mode had **no keyboard controls**: pause / flatten /
kill-switch / resume only existed in the terminal UI. That is now **done**: `python -m aeon --ctl ...`
(local, token-protected; see `DOCKER.md` Section 5).

---

## 2. What the codebase is

An autonomous LLM trading agent for OKX perpetual futures: every 5–60 minutes it scans ~290 USDT
perps, renders a compact market snapshot into a prompt, gets one JSON decision back from the model,
runs each proposed action through a code-level risk governor, executes what survives, and journals
everything to SQLite. Between cycles a 5-second monitor loop handles stops, liquidations and equity.

### Module map

| Module | Lines (approx) | Role |
|---|---|---|
| `__main__.py` | 220 | CLI (`--check --headless --once --mode --brain`), wiring, `Setup` |
| `agent.py` | 400 | the 24/7 loop: monitor + decision cycles + execution |
| `brain.py` | 395 | system prompt, LLM client (retries/JSON repair), parser, heuristic baseline |
| `risk.py` | 286 | the governor: sizing caps, stop rules, kill switch — **runs after the model decides** |
| `okx_broker.py` | 288 | demo/live broker: market orders + exchange-side stops, re-arm loop |
| `broker.py` | 236 | paper broker (fills, fees, slippage, stops, liquidation) + shared types |
| `okx.py` | 171 | minimal async OKX v5 client (public + signed) |
| `market.py` | 187 | universe scan, contract specs, indicator snapshot |
| `journal.py` | 96 | SQLite journal (kv, equity, cycles, actions, trades) |
| `feed.py` | 87 | WebSocket price feed with REST fallback |
| `config.py` | 150 | strict config loader (unknown keys rejected) |
| `tui.py` | ~700 | the Textual cockpit |
| `indicators.py` | 49 | EMA / RSI / ATR, dependency-free |

### What is genuinely good

1. **The single most important design decision is done right.** The model picks *what* to do; the
   risk limits are enforced in `risk.py` *after* it decides, in code it cannot argue past. Every
   `open/add/reduce/close/adjust` goes through an evaluator that can reject or clip it. This is the
   correct architecture for an LLM-in-the-loop trading system, and it's consistently applied.
2. **Fail-safe thinking around real money.**
   * Stops are attached to the entry order and live **on OKX's servers** — a crash or dropped
     network doesn't leave a position unprotected.
   * `OkxBroker.tick()` re-checks every position each tick and re-arms a missing stop; if it can't
     restore it within 60 s it **closes the position**.
   * Isolated margin only, so one bad trade can't eat the whole account.
   * `mode=live` refuses to start without an explicit `AEON_CONFIRM_LIVE` phrase, and `--check`
     actively fails if your API key has **withdraw** permission.
   * Orders are never retried automatically (a retried POST could double-fill) and every order
     carries a unique `clOrdId`.
3. **Crash resilience in every loop.** The monitor loop catches-all so it can't die; the decision
   loop backs off exponentially on LLM failures (60 s → 600 s) and tells you your stops are still
   working; the feed falls back from WebSocket to REST and skips a cycle if prices are stale rather
   than trading on old data; JSON parsing tolerates markdown fences, `<think>` blocks and a full
   retry.
4. **State survives restarts.** Kill-switch flag, drawdown peak, daily counters, re-entry
   cooldowns, paper balance/positions, agent notes and the token-cost meter are all persisted in
   SQLite and reloaded on boot.
5. **Strict config.** Unknown keys are *rejected* (a typo in `max_drawdown_pct` fails loudly instead
   of silently doing nothing) with type checking. This is rare and it matters for a risk config.
6. **Honest tests.** 35 tests covering the parts that lose money if wrong: risk math, paper
   accounting, order payload shape, prompt/JSON parsing, UI helpers. They run in <1 s.

### What I'd flag

| # | Issue | Severity | Notes |
|---|---|---|---|
| 1 | **Headless mode had no control channel.** Pause / Think-now / Flatten / Kill-switch / Resume were TUI key bindings only. | **High for your use case** | **Addressed:** `python -m aeon --ctl status\|pause\|unpause\|cycle\|flatten\|kill\|resume` talks to the running daemon over a loopback-only, token-protected channel (`aeon/control.py`). Kill state persists; `resume` resets the drawdown baseline like the TUI's `R`. See `DOCKER.md` §5. |
| 2 | **No alerts** was the gap: nothing told you the process died, the LLM had failed for an hour, or the kill switch fired — you only found out by reading logs. | High when unattended | **Addressed:** a Discord notifier (`aeon/notify.py`, `[notify]` in `config.toml`) now posts fills, governor decisions, errors and a boot message as clean embeds. See `README.md` → Discord alerts. |
| 3 | **No observability surface.** No web view, no health endpoint. You read `journalctl` and query `aeon.db`. | Medium | Fine for a first version; a read-only status page would make hosting much more comfortable. |
| 4 | **UI and agent share one process.** A headless server therefore has no cockpit (README says this explicitly). | Medium | Workaround: run the TUI inside `tmux` over SSH (§4, option B). |
| 5 | `okx_broker.py` signed calls were **never tested against a real OKX account** (README is upfront: tested for request shape only). | Medium | Must be confirmed in `demo` mode — ideally *from the server's IP* before any live money. |
| 6 | `equity` table grows unbounded (~1 row/min ≈ 525k rows/year ≈ a few MB). No pruning/backup story. | Low | Trivial to add retention; back up `aeon.db` if you care about history. |
| 7 | `feed.py` swallows WebSocket exceptions silently (`except Exception` with no logging) when it drops to REST. | Low | You see `feed REST` in the UI but headless gives no event for the downgrade. |
| 8 | Only one process may ever run against a given journal (two agents = double trading). | Info | **Now enforced:** a lock file next to the journal makes a second agent refuse to start. |

Nothing here is a "this code is dangerous" finding. It's careful, defensively written code with a
sound risk architecture; the gaps are operational (controls, alerting), not algorithmic.

---

## 3. What hosting it requires

| Requirement | Reality |
|---|---|
| Process type | One long-running Python process (WebSocket + async loops) → **not** serverless |
| Network | Outbound only: `www.okx.com`, `ws.okx.com:8443`, your LLM endpoint. **No inbound ports.** |
| Disk | Local disk for `aeon.db` (SQLite WAL — do **not** put it on a network filesystem) |
| CPU / RAM | Negligible: 1 vCPU / 512 MB is plenty; 1 GB is comfortable |
| Region | Pick a VPS region where **you are allowed to trade on OKX** and that OKX serves — check both your own residency rules and OKX's supported-jurisdiction list before choosing. (E.g. US IPs are not usable for OKX trading.) |
| Cost | VPS ~$4–6/mo + LLM: DeepSeek-class model ≈ **$3–5/mo** at 96 cycles/day; a frontier model ≈ **$30/mo** (per README) |
| IP allow-list | If your OKX API key is IP-restricted (it should be), **add the server's IP** or all private calls fail |

---

## 4. Hosting options

### Option A — Small VPS with systemd *(recommended)*

A $4–6 VPS (Hetzner, OVH, DigitalOcean, Vultr, AWS Lightsail, …) running Ubuntu/Debian.
Full control, static IP for the OKX allow-list, SSH access, `journalctl` logs, local SQLite,
and you can run the TUI over SSH when you want the cockpit.

```bash
# one-time setup on the server
sudo apt update && sudo apt install -y python3.11-venv git
git clone <your-repo> /opt/aeon && cd /opt/aeon      # or scp the folder
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env && nano .env                    # keys; then: chmod 600 .env
.venv/bin/python -m aeon --check                     # must print "ready"
```

```ini
# /etc/systemd/system/aeon.service
[Unit]
Description=AEON autonomous perp trader (headless)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/aeon          # required: config.toml and aeon.db are relative paths
EnvironmentFile=/opt/aeon/.env
ExecStart=/opt/aeon/.venv/bin/python -m aeon --headless
Restart=always
RestartSec=10
# log rotation is handled by journald
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aeon
journalctl -u aeon -f              # watch it think
```

**Optional cockpit over SSH:** run the TUI instead of headless inside a terminal multiplexer so it
survives your disconnect — and keep the keyboard controls:

```bash
sudo systemctl stop aeon           # never run both at once: two agents = double trading
tmux new -s aeon
/opt/aeon/.venv/bin/python -m aeon     # full TUI, keys P/C/M/F/K/R/Q all work
# detach: Ctrl-b d   re-attach later: tmux attach -t aeon
```

### Option B — Docker on your own VPS *(built)*

The repo now ships a `Dockerfile`, `.dockerignore` and `docker-compose.yml`, with a full walkthrough in
**`DOCKER.md`**. What it gets right, if you read nothing else:

* **Persistent volume for the journal.** The image runs from `/data` and a named volume is mounted
  there, so the journal (and the kill-switch flag and paper balance) survives rebuilds and redeploys.
  `config.toml` is bind-mounted read-only; secrets come from `.env` at run time, never baked in.
* **One replica by design** (a journal lock enforces it), `restart: unless-stopped`, a clean `SIGINT`/`SIGTERM` shutdown,
  a healthcheck, and a watchdog that exits a wedged agent so Docker restarts it.
* **Hardened and pinned:** read-only root filesystem, no capabilities, memory cap, dependencies pinned in `requirements.lock`.
* **Go-live gates, runbooks and backups** are in `DOCKER.md`, with an honest list of what is not verified.
* **No free/sleeping tier.** A sleeping process is a paused trader.

```bash
git clone <repo> && cd aeon
cp .env.example .env && chmod 600 .env && nano .env     # keys + Discord webhook
docker compose build
docker compose run --rm aeon --check                    # verify before trading
docker compose up -d                                    # 24/7
```

### Option C — Not suitable

* **Serverless** (AWS Lambda, Cloud Functions, Vercel, Cloudflare Workers): needs a persistent
  WebSocket and long-lived loops; stateful and long-running by nature.
* **Your laptop with sleep / any machine you turn off**: exactly the problem you described. Note
  the mitigations that *do* exist today: in `demo`/`live`, stops sit on OKX so open positions stay
  protected while AEON is off — but paper positions freeze and the agent stops thinking.

---

## 5. Decide this before you deploy

These are the open choices, in priority order:

1. **How do you control it remotely?** ✅ **Done: option A**, as a local command rather than an HTTP endpoint
   (`python -m aeon --ctl ...`, loopback listener + token file, no port to tunnel; `DOCKER.md` §5). The options
   as originally listed:
   * **A. I add a control channel**: a tiny localhost HTTP endpoint (or stdin commands) for
     `pause / resume / flatten / kill / status`, callable over SSH tunnel. Small, clean change
     (~50 lines in `agent.py` + `__main__.py`). ← my recommendation
   * **B. tmux over SSH**: run the TUI on the server (Option A's variant). Full controls, no code
     changes, but you must SSH in to see or touch anything.
   * **C. Do nothing**: headless + logs only. Fine while everything works; bad the day the kill
     switch fires at 03:00.
2. **Do you want alerts?** ✅ **Decided: Discord.** Implemented as a webhook notifier (`aeon/notify.py`)
   that posts a fill, kill-switch/daily-limit, LLM failure and process-start message as a clean embed.
   Configure `[notify]` in `config.toml`, put `DISCORD_WEBHOOK_URL` in `.env`, and run
   `python -m aeon --check` to send yourself a test message. This is the difference between
   "unattended" and "unmonitored".
3. **Paper, demo or live on the server?** Recommendation matches the README's: host it in **paper**
   first (no OKX keys needed at all, so you can validate the hosting itself for free), then **demo**
   *from the server IP*, and only then live.
4. **Which model?** Cost on a 200 EUR account is real: DeepSeek-class ≈ $3–5/mo, frontier ≈ $30/mo.
   Fill the price fields in `config.toml` so the BRAIN tile shows true spend.

---

## 6. Deployment checklist

```
[ ] VPS region is one where you and OKX are both OK to trade
[ ] Server has Python ≥3.11;  pip install -e .  works;  pytest  -> all pass (219 at the time of writing)
[ ] .env on server: chmod 600, never committed, never pasted into chat
[ ] OKX API key: Trade permission ONLY, no Withdraw, IP allow-list = server IP
[ ] python -m aeon --check  prints "ready" FROM THE SERVER
[ ] Run paper mode for a few days on the server; confirm restarts behave
[ ] Run demo mode from the server IP; confirm entries/stops/partial closes/stop-outs
[ ] systemd (or Docker) with Restart=always;  systemctl enable
[ ] db_path on local disk;  aeon.db backed up (cron + scp/rsync)
[ ] Control channel tested on the server: `--ctl status`, pause, flatten, kill, resume (DOCKER.md §5)
[ ] Discord alerts tested (`python -m aeon --check` posts a real message to the channel)
[ ] Only ONE instance ever runs against this db_path
[ ] Cost watch: BRAIN meter > your limit -> change model / raise cycle gap
```

---

## 7. Bottom line

* **The code**: above-average for this kind of project — the risk-after-decision architecture,
  exchange-side stops with a fail-safe re-arm loop, persisted kill state and strict config are the
  right instincts, and the tests cover what actually loses money. The weaknesses are operational
  (no web view; remote controls and alerts now exist), not structural.
* **Hosting**: absolutely possible and effectively pre-designed for it (`--headless`, outbound-only,
  SQLite state, restart-safe). Cheapest sane path is a ~$5 VPS under `systemd`; Docker/PaaS works
  with a persistent volume; serverless does not.
* **The thing that stood between "runs in the cloud" and "I can live with it running in the cloud"**
  was a remote way to pause/kill/resume (item 1 in §5). It now exists (`--ctl`, `DOCKER.md` §5), and getting
  alerted is handled by the Discord notifier (item 2).

The remote control (§5.1), Discord alerts (§5.2) and the Docker deployment (`Dockerfile` +
`docker-compose.yml`, see `DOCKER.md`) are **done**. None of it has run against a real OKX account yet.
