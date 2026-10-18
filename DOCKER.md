# Running AEON on your own VPS with Docker

One container, one agent, one journal, restart-safe. This file is the deployment guide **and** the
go-live checklist. Read "What is and is not verified" before putting real money behind it.

- **Image**: `Dockerfile` (Python 3.12, dependencies pinned in `requirements.lock`, unprivileged user, healthcheck)
- **Run config**: `docker-compose.yml` (read-only root filesystem, no capabilities, memory cap, named volume, `.env` for secrets)
- **State**: the SQLite journal lives in the `aeon-data` volume at `/data/aeon-<mode>.db`
- **Safety rail**: a second AEON on the same journal refuses to start (lock file), so `docker compose run` can
  never double-trade next to the daemon

---

## 0. What is and is not verified

| Verified | How |
|---|---|
| 219 offline tests pass | `pytest` against fakes, including a fake OKX with lost replies, partial fills, stop firing |
| The headless process end to end | Run for real on Windows in paper mode: heartbeat written, healthcheck passes, a second agent on the same journal is refused, the lock is released when the first dies |
| Backup logic | Run against a live WAL journal with an open writer: consistent, `integrity_check` ok |
| `docker-compose.yml` is valid YAML with the hardening keys; the lock file resolves for linux / CPython 3.12 with wheels only | Parsed / `pip install --dry-run --platform manylinux2014_x86_64` |

| **Not** verified | What to do |
|---|---|
| The Docker image was never **built or run** (no Docker on the dev machine) | First step on the VPS: `docker compose build`, then check `docker inspect` health reaches `healthy` |
| The terminal UI inside the container (`--tui`, Section 5) on a read-only filesystem | Try it at Gate 3 item 7; if it will not start, run the TUI from a Python virtualenv on the VPS instead, with the daemon stopped |
| SIGTERM handling and `flock` on Linux (written, tested on Windows only) | Gate 2 below: `docker compose stop` must log `stopping; open positions keep their exchange-side stops` |
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
| **4. Live, small** | Section 6 | First week with deliberately tight limits (below) and you watching Discord |
| **5. Scale** | Only after a clean week | Raise limits one at a time |

**Gate 3: watch for each of these at least once** (Discord / `docker compose logs`, and the OKX demo web UI):
1. An entry whose stop-loss and take-profit appear in OKX under *Orders > Algo orders*.
2. A stop or target firing on OKX and AEON booking it (`stop/target hit on exchange`) with a believable P&L.
3. `docker compose restart` while a position is open: log says `restored saved state`, exactly **one** stop remains on OKX.
4. A VPS reboot (`sudo reboot`): the container comes back by itself, `docker inspect` shows `healthy`.
5. `HEARTBEAT_URL` alert actually reaching you after `docker compose stop` (Section 7).
6. A spot buy, its stop, and the stop firing.
7. A flatten and a kill + resume, done from the TUI (Section 5).

**Gate 4 first-week limits** (edit `config.toml`, then `docker compose restart`): `max_leverage = 2`,
`risk_per_trade_pct = 0.5`, `max_open_positions = 2`, `budget_usdt` = only what you can lose. AEON's
`budget_usdt` is a sizing cap, **not isolation**: fund a dedicated OKX **sub-account** with just that amount.

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

### Alert: "Kill switch / halt" (HALTED)
The drawdown limit fired: AEON flattened everything and will not trade until **you** resume it. Read the log and the
Discord history first. To resume you need the terminal UI (headless has no control channel yet, see Section 8):

```bash
docker compose stop
docker compose run --rm -it aeon --tui      # review, then press R (resets the drawdown baseline to now), then Q
docker compose start
```

The same TUI gives you `F` flatten, `K` kill switch, `P` pause. Run it only while the daemon is stopped.

### Emergency: get out of the market now
There is no remote kill switch. The fastest way out does not depend on AEON: **close the positions and cancel the
algo orders in the OKX app or website.** Then `docker compose stop`. (AEON books a hand-closed position as
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
3. `.env`: the live key and `AEON_CONFIRM_LIVE=I-ACCEPT-REAL-MONEY-RISK`. `config.toml`: `mode = "live"` plus the
   first-week limits from Section 1. `docker compose up -d --force-recreate`.
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

- **No remote control channel in headless mode** (pause / flatten / kill / resume are TUI keys). Workarounds above.
  Building one is an open decision in `HOSTING.md` Section 5.
- Perpetual and future stops are replaced by cancel-then-place, so there is a short gap; the 5-second monitor re-arms
  a missing stop and closes the position if it cannot within 60 seconds. Covered size and side of an existing stop are
  not verified (`CODEBASE_REVIEW.md` F4). A close that OKX acknowledges but does not complete is not re-checked (F8).
  Partial-reduction P&L on derivatives is an estimate (F12).
- Hedge (long/short) position mode is refused at start. Net mode only.
- `budget_usdt` caps what AEON sizes against; it does not wall off the rest of the account. Use a sub-account.
- `stop_grace_period` is 30 s. A hard kill (`kill -9`, power loss) can lose the last seconds of unsaved state; unconfirmed
  orders are saved *before* they are sent and reconciled on the next start.
- A model or provider outage only stops new decisions. Open positions keep their exchange-side stops.
