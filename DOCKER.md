# Running AEON on your own VPS with Docker

One container, one agent, one journal, restart-safe. This file is the deployment guide **and** the
go-live checklist. Read "What is and is not verified" before putting real money behind it.

- **Image**: `Dockerfile` (Python 3.12, dependencies pinned in `requirements.lock`, unprivileged user, healthcheck)
- **Run config**: `docker-compose.yml` (read-only root filesystem, no capabilities, memory cap, named volume, `.env` for secrets)
- **State**: the SQLite journal lives in the `aeon-data` volume at `/data/aeon-<mode>.db`
- **Safety rail**: a second AEON on the same journal refuses to start (lock file), so `docker compose run` can
  never double-trade next to the daemon
- **Control**: `docker compose exec aeon python -m aeon --config /app/config.toml --ctl status|pause|unpause|cycle|flatten|kill|resume`
  talks to the running daemon over a loopback-only, token-protected channel (Section 5). No port is published

---

## 0. What is and is not verified

| Verified | How |
|---|---|
| 345 offline tests pass (2 more are skipped on Windows: POSIX file modes) | `pytest` against fakes, including a fake OKX with lost replies, partial fills, stop firing, the control channel's auth, limits, confirmation and resume semantics, and the terminal UI at sizes from 200x50 down to 64x18 in Textual's headless harness |
| The headless process end to end | Run for real on Windows in paper mode: heartbeat written, healthcheck passes, a second agent on the same journal is refused, the lock is released when the first dies |
| The control channel against a real headless process | Run on Windows in paper mode with the real CLI: `status`, `pause` (twice), `unpause`, `cycle`, `flatten` with and without `--yes`, `kill`, `resume` with and without a halt, wrong `--mode`, and a stale file after a hard kill |
| Backup logic | Run against a live WAL journal with an open writer: consistent, `integrity_check` ok |
| `docker-compose.yml` is valid YAML with the hardening keys; the lock file resolves for linux / CPython 3.12 with wheels only | Parsed / `pip install --dry-run --platform manylinux2014_x86_64` |

| **Not** verified | What to do |
|---|---|
| The Docker image was never **built or run** (no Docker on the dev machine) | First step on the VPS: `docker compose build`, then check `docker inspect` health reaches `healthy` |
| The terminal UI inside the container (`--tui`, Section 5) on a read-only filesystem | Try it at Gate 3 item 7; if it will not start, run the TUI from a Python virtualenv on the VPS instead, with the daemon stopped |
| SIGTERM handling and `flock` on Linux (written, tested on Windows only) | Gate 2 below: `docker compose stop` must log `stopping; open positions keep their exchange-side stops` |
| The control channel on Linux and inside the container: the token file really is `0600` and owned by uid 1000, `docker compose exec` (as uid 1000 and as root) reaches the daemon, the file disappears on `docker compose stop` | Gate 2, once: `docker compose exec aeon ls -l /tmp` then `aeonctl status` (Section 5); the two POSIX-only tests run on Linux (`pytest tests/test_control.py`) |
| **The OKX order path has never run against a real exchange, not even demo.** The demo journal on the dev machine shows 4 decision cycles in 25 hours and zero trades | `--smoke-demo` (Gate 2) then a demo soak (Gate 3) |
| Anything about profitability | Nothing here tests it. Treat the money as an experiment you can lose |

---

## 1. Rollout gates: do not skip one

| Gate | Do | Pass when |
|---|---|---|
| **0. Prepare the VPS** | Section 2 | Hardening checklist done; clock synced; image builds |
| **1. Verify** | `docker compose run --rm aeon --check` | Every line `[ok]`: OKX public + private reachable, **clock within 5 s of OKX**, key has no Withdraw, LLM replies, Discord test message arrives |
| **2. Smoke test (demo)** | `docker compose run --rm aeon --smoke-demo` with `mode = "demo"`, daemon stopped | `SMOKE TEST PASSED`: perpetual and spot legs both buy, show stop and target on the exchange, move the stop, close, and end flat. Then `docker compose up -d` and `docker compose stop`: the log must show a clean stop |
| **3. Demo soak, 7+ days on the VPS** | Leave it running in `demo` | See the list below, all observed at least once |
| **4. Live, small** | Section 6 | First week on the shipped limits (below) and you watching Discord |
| **5. Scale** | Only after a clean week | Raise limits one at a time |

**Gate 3: watch for each of these at least once** (Discord / `docker compose logs`, and the OKX demo web UI):
1. An entry whose stop-loss and take-profit appear in OKX under *Orders > Algo orders*.
2. A stop or target firing on OKX and AEON booking it (`stop/target hit on exchange`) with a believable P&L.
3. `docker compose restart` while a position is open: log says `restored saved state`, exactly **one** stop remains on OKX.
4. A VPS reboot (`sudo reboot`): the container comes back by itself, `docker inspect` shows `healthy`.
5. `HEARTBEAT_URL` alert actually reaching you after `docker compose stop` (Section 7).
6. A spot buy, its stop, and the stop firing.
7. A flatten, a pause and unpause, and a kill + resume, done with `--ctl` (Section 5), each confirmed by `--ctl status` and the Discord audit line.

**The shipped `config.toml` already is the first-week set**, for demo and live alike, so what you soak is what goes
live and there is nothing to edit: demo mode, `max_leverage = 2`, `risk_per_trade_pct = 0.5`, `max_open_positions = 2`,
daily loss limit 2%, drawdown halt 10%, 10 entries a day, perpetuals and spot only (dated futures are opt-in:
nothing exercises them on the exchange yet). The one number that is yours is `budget_usdt` (default 200): for live,
set it to what you can lose. It is a sizing cap, **not isolation**: fund a dedicated OKX **sub-account** with just that
amount. Gate 5 loosens one limit at a time; `config.toml` carries the next-step value beside each.

---

## 2. Prepare the VPS (once)

Any Debian/Ubuntu VPS with 1 vCPU / 1 GB is plenty. Pick a region where you **and** OKX are allowed to trade.

```bash
# as root, once: a normal user with SSH keys, then stop using root
adduser deploy && usermod -aG sudo deploy        # copy your public key to ~deploy/.ssh/authorized_keys
```

Then as `deploy`:

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install ufw fail2ban unattended-upgrades chrony ca-certificates curl

# SSH: keys only, no root login   (edit /etc/ssh/sshd_config, then: sudo systemctl restart ssh)
#   PasswordAuthentication no
#   PermitRootLogin no

# Firewall: AEON makes outbound connections only and listens on nothing, so deny everything inbound but SSH
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow OpenSSH && sudo ufw enable

# Clock: signed OKX requests are refused if the clock drifts (~30 s). Must say "synchronized: yes".
sudo systemctl enable --now chrony && timedatectl | grep -i synchronized

# Docker, started at boot
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker && sudo usermod -aG docker "$USER"     # log out and back in
docker compose version
```

Notes: nothing is published by `docker-compose.yml`, so Docker's habit of bypassing `ufw` does not matter here.
Do **not** add a port mapping to this service. The OKX API key should be restricted to the VPS's public IPv4
(`curl -4 ifconfig.me`).

## 3. Deploy

```bash
git clone <your-repo-url> /opt/aeon && cd /opt/aeon      # or scp the folder; .gitattributes keeps scripts LF
cp .env.example .env && chmod 600 .env && nano .env      # keys only, KEY=value lines, no old keys in comments
chmod 644 config.toml                                    # the container user (uid 1000) must be able to read it
mkdir -p backups && sudo chown 1000 backups              # for scripts/backup.sh
```

`.env` holds `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`, `LLM_API_KEY`, `DISCORD_WEBHOOK_URL`, the optional
`HEARTBEAT_URL`, and (live only) `AEON_CONFIRM_LIVE`. Create the OKX key with **Trade** permission only, never Withdraw.
Edit `config.toml` on the host (mounted read-only): start with `mode = "demo"`.

```bash
docker compose build
docker compose run --rm aeon --check            # Gate 1
docker compose run --rm aeon --smoke-demo       # Gate 2 (demo mode only; refuses otherwise)
docker compose up -d
docker compose logs -f --tail=100               # Ctrl-C stops watching, not the agent
docker inspect -f '{{.State.Health.Status}}' aeon     # "starting" for ~3 min, then "healthy"
```

## 4. Day-to-day

| I want to... | Do this |
|---|---|
| See what it is doing | `docker compose logs -f --tail=100` |
| Ask it: state, equity, positions with stops, halt reason | `aeonctl status` (alias in Section 5) |
| Pause, flatten, kill or resume it | `aeonctl pause`, `aeonctl flatten --yes`, `aeonctl kill --yes`, `aeonctl resume --yes` (Section 5) |
| Check health and restarts | `docker inspect -f '{{.State.Health.Status}} restarts={{.RestartCount}}' aeon` |
| Stop it (positions keep their exchange-side stops) | `docker compose stop` |
| Start it again | `docker compose start` |
| Apply a new `config.toml` | edit, then `docker compose restart` (a config error shows in the logs and the container retries until fixed) |
| Apply a changed `.env` (new keys) | `docker compose up -d --force-recreate`  (`restart` does **not** re-read `.env`) |
| Update the code | see "Upgrade and roll back" |
| One paper cycle without touching the real journal | `docker compose run --rm aeon --mode paper --brain heuristic --once` |

`--check` never opens the journal, and a `--mode paper` run uses its own journal (`aeon-paper.db`), so both are safe
next to a demo or live daemon. `--smoke-demo` needs the daemon stopped (it would otherwise adopt the test position).
Anything else aimed at the daemon's own journal refuses with `another AEON process is already using the journal`.

## 5. Runbooks

### Remote control: status, pause, flatten, kill, resume
The running daemon answers a local command, so you no longer need the terminal UI. Run it on the VPS (over SSH).
Set this alias once; `-T` keeps the output clean and makes it work from scripts and cron:

```bash
alias aeonctl='docker compose exec -T aeon python -m aeon --config /app/config.toml --ctl'
aeonctl status
```

| Command | What it does |
|---|---|
| `status` | Mode, `RUNNING` / `PAUSED` / `HALTED`, equity, open positions with stop and target, drawdown / day P&L / exposure, the halt reason, last decision time, and how long ago the monitor last checked in (`stalled_for`). The stop and target are **what AEON has on record**, not a fresh read from OKX (`CODEBASE_REVIEW.md` F4) |
| `pause` / `unpause` | Stop / start new decision cycles. Open positions keep their stops. Repeating either is harmless. **Not saved**: a restart (watchdog, `docker compose restart`, reboot) comes back unpaused |
| `cycle` | One decision cycle now (the TUI's `C`). Refused while halted or while a decision is already running |
| `flatten --yes` | Close every open position at market. Trading continues afterwards |
| `kill --yes` | Close every position and halt. Saved: it survives restarts until `resume` |
| `resume --yes` | Lift a halt **and reset the drawdown baseline to current equity**, exactly as `R` does in the TUI. Refused when not halted, so it can never quietly reset a live drawdown |

`flatten`, `kill` and `resume` do nothing without `--yes` (the agent checks it too, not just the CLI). Each one writes
`control channel: <command> requested` to the log and to Discord before it runs. Exit code: `0` done, `1` the agent
refused (for example `nothing to resume`), `2` could not talk to it or `--yes` missing.

It reads the mode from `config.toml` like `--healthcheck` does. If you started the daemon with a different `--mode`,
pass the same flag; the error message tells you when a control file for another mode exists.

**How it is kept safe**
- No published port, and nothing listens on the network: the listener is `127.0.0.1` inside the container on a random
  port. **Never add `ports:` or `network_mode: host` to this service**: host networking would put that listener on
  the VPS's own loopback, where any local user could try it (they would still need the token).
- Every request must carry a random token, new at each start, in `/tmp/aeon-<mode>.control` (the container's
  private tmpfs, mode `0600`, owned by uid 1000, deleted on stop). It is never logged, printed, echoed or put on a
  command line. The client refuses a token file that is not a regular file, not its own (root excepted), or readable by others.
- Anyone who can `docker exec` into the container, or read that file as root, can drive the trader. Treat membership
  of the `docker` group on the VPS as access to the account.
- Requests are one JSON line of at most 4 KB, read within 5 s, from at most 4 connections at once. `flatten` and
  `kill` keep running to completion even if your SSH session drops; a command that takes over 90 s answers "still
  working" and carries on (check `status`).
- `AEON_CONTROL=off` in `.env` (then `docker compose up -d --force-recreate`) turns the whole thing off.
- If the channel cannot start (for example the tmpfs is full) the trader keeps running and an `error` alert says
  that pause / flatten / kill / resume need the terminal UI until the next restart.

### Alert: "Kill switch / halt" (HALTED)
The drawdown limit fired (or someone ran `kill`): AEON flattened everything and will not trade until **you** resume it.
Look first, resume second:

```bash
aeonctl status                          # HALTED: <reason>; positions should be flat
docker compose logs --tail=200 aeon     # what happened; compare with the Discord history and the OKX web UI
aeonctl resume --yes                    # only when you understand why: restarts the drawdown clock from current equity
```

Resuming re-bases the drawdown limit to the equity you have **now**, so each resume after a loss lets AEON lose
another full drawdown limit. If you are not sure, leave it halted. If `aeonctl` cannot connect, the fallback is the
terminal UI with the daemon stopped (`F` flatten, `K` kill, `R` resume, `P` pause):

```bash
docker compose stop
docker compose run --rm -it aeon --tui      # review, then press R, then Q
docker compose start
```

### Emergency: get out of the market now
If AEON is healthy (`aeonctl status` answers): `aeonctl kill --yes` closes everything and keeps it from trading again.
The route that does not depend on AEON at all is still the fastest and the one to use when `aeonctl` cannot connect,
the container is `unhealthy`, or you do not trust what AEON reports: **close the positions and cancel the algo orders
in the OKX app or website.** Then `docker compose stop`. (AEON books a hand-closed position as
`stop/target hit on exchange`; that is expected.) `docker compose stop` alone only stops new decisions: open
positions stay open, protected by their exchange-side stops.

### Container `unhealthy`, or restarting
```bash
docker inspect -f '{{.State.Health.Status}} restarts={{.RestartCount}} exit={{.State.ExitCode}}' aeon
docker compose logs --tail=200 aeon
```
Exit code **70** = the watchdog ended a wedged agent (no monitor heartbeat for 10 minutes) and Docker restarted
it. Exit code **1** with `startup failed` = OKX unreachable, bad keys, hedge-mode account, or an IP not on the key's
allow-list: fix the cause, the container keeps retrying with Docker's back-off. Repeated "AEON online" messages in
Discord mean restarts.

### Lost or corrupt journal
The journal holds the kill-switch flag, drawdown peak, stops on record and spot holdings. **Restore from a backup if
you have one** (below). Starting fresh with positions still open leaves AEON blind to the stops it had: it will not
re-arm a stop it does not know about, and a halted account would come back un-halted. Close open positions on OKX first.

### Backups
```bash
./scripts/backup.sh            # online and consistent, safe while running; gzip, keeps 14 days in ./backups
```
Schedule it and copy the folder **off the server** (rclone, rsync or scp): a backup on the same disk dies with the disk.

```cron
17 */6 * * *  cd /opt/aeon && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Restore (stop first; a stale `-wal` file would corrupt the restored copy, so it is removed):

```bash
docker compose stop
gunzip -k backups/aeon-demo-20261019T020000Z.db.gz
docker compose run --rm --no-deps -T -v "$PWD/backups:/in:ro" --entrypoint sh aeon -c \
  'rm -f /data/aeon-demo.db-wal /data/aeon-demo.db-shm && cp /in/aeon-demo-20261019T020000Z.db /data/aeon-demo.db'
docker compose start      # then watch the first minutes of logs: positions are reconciled against OKX
```

### Upgrade and roll back
```bash
git pull
docker tag aeon:latest aeon:previous            # keep the running image for rollback
docker compose up -d --build
docker compose logs -f --tail=50                # expect "AEON online"; health becomes healthy within ~3 minutes
# roll back:
docker tag aeon:previous aeon:latest && docker compose up -d --no-build
```
Upgrade while flat if you can. State and open positions survive a restart by design, but a restart is still a
restart.

## 6. Going live (Gate 4)

1. Gates 1-3 passed on this VPS, in `demo`.
2. A dedicated OKX **sub-account**, funded with only the budget; account mode *Spot and futures* or *Multi-currency
   margin*, **position mode = Net** (AEON refuses hedge mode), API key **Trade-only**, restricted to the VPS IP.
3. `.env`: the live OKX key, `AEON_MODE=live` and `AEON_CONFIRM_LIVE=I-ACCEPT-REAL-MONEY-RISK`. `config.toml` needs no
   edit apart from `budget_usdt` (set it to the amount in the sub-account); the limits are already the first-week set.
   `docker compose up -d --force-recreate` (`restart` does not re-read `.env`).
4. Stay on Discord for the first sessions. The live journal is separate (`aeon-live.db`), so demo history does not carry over.

## 7. Monitoring: how you find out it is not running

- **Discord** tells you what AEON does and when it halts. It goes silent *together with* AEON, so it cannot tell you
  the process or the VPS died.
- **`HEARTBEAT_URL`**: create a free "heartbeat/cron" monitor (healthchecks.io, Better Stack, UptimeRobot), set its
  expected period to ~15 minutes, paste its https ping URL into `.env`. AEON GETs it every 5 minutes while healthy;
  when pings stop you get an email/SMS. Only an empty GET is sent.
- **The watchdog** (`AEON_WATCHDOG_S`, default 600, `0` disables) restarts a wedged agent. Docker's `healthy` status
  alone does **not** restart anything.
- Watch disk space (`df -h`) and keep unattended upgrades on. Logs are capped at 5 x 10 MB by Docker.

## 8. Known limitations (honest list)

- **The control channel is local and only as alive as the agent.** You reach it by SSH to the VPS plus `docker compose
  exec`; there is deliberately no remote endpoint. It runs inside the agent's event loop, so a wedged agent cannot
  answer (the watchdog restarts it after `AEON_WATCHDOG_S`, default 10 minutes; use the OKX app meanwhile). `pause` is not
  saved, so any restart resumes deciding; `kill` is. `flatten` / `kill` report what AEON's own account view shows
  after closing, and a close that OKX acknowledges but does not complete is not re-checked (F8): confirm on OKX after
  an emergency. Not yet run on Linux or in the container (Section 0).
- Perpetual and future stops are replaced by cancel-then-place, so there is a short gap; the 5-second monitor re-arms
  a missing stop and closes the position if it cannot within 60 seconds. Covered size and side of an existing stop are
  not verified (`CODEBASE_REVIEW.md` F4). A close that OKX acknowledges but does not complete is not re-checked (F8).
  Partial-reduction P&L on derivatives is an estimate (F12).
- Hedge (long/short) position mode is refused at start. Net mode only.
- `budget_usdt` caps what AEON sizes against; it does not wall off the rest of the account. Use a sub-account.
- `stop_grace_period` is 30 s. A hard kill (`kill -9`, power loss) can lose the last seconds of unsaved state; unconfirmed
  orders are saved *before* they are sent and reconciled on the next start.
- A model or provider outage only stops new decisions. Open positions keep their exchange-side stops.
