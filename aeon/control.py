"""Local control channel for headless mode: status / pause / unpause / cycle / flatten / kill / resume.

Headless AEON has no keyboard, so the TUI keys (P, C, F, K, R) had no equivalent on a server. This module is that
equivalent, built so that it is hard to misuse on a real-money trader:

  * the server listens on 127.0.0.1 only, on an ephemeral port. Nothing is published by Docker and nothing is
    reachable from the network; inside a container "127.0.0.1" is the container's own loopback;
  * every request must carry a random token that lives in a 0600 file next to the heartbeat file. Whoever can
    read that file (the AEON user, or root) can drive the agent; nobody else can, even from the same host;
  * the token is never logged, never printed, never put on a command line, and never echoed in a reply;
  * requests are one JSON line, bounded in size and time, one request per connection, a handful of connections;
  * flatten / kill / resume do nothing without an explicit confirmation, enforced here on the server;
  * it only calls the Agent methods the TUI keys call (toggle_pause, cycle_now, flatten, kill, resume).

Wire format: the client sends one line  {"token": "...", "cmd": "status", "confirm": false}  and gets one line back,
{"ok": true, ...} or {"ok": false, "error": "..."}, then the connection closes.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import secrets
import stat
import sys
import time
from pathlib import Path

from .health import heartbeat_path

HOST = "127.0.0.1"                      # never anything else: see the module docstring
MAX_REQUEST_BYTES = 4096                # a real request is ~100 bytes
MAX_REPLY_BYTES = 262_144               # the client's cap on what it will read back
MAX_CLIENTS = 4                         # simultaneous connections; extras are turned away
READ_TIMEOUT_S = 5.0                    # to receive the request line
SLOW_TIMEOUT_S = 90.0                   # flatten / kill talk to the exchange; the work itself carries on past this
CLIENT_PAD_S = 10.0                     # the client waits this much longer than the server does
CONNECT_TIMEOUT_S = 5.0
JOB_GRACE_S = 15.0                      # at shutdown, how long a flatten that is already running may finish

READ_ONLY = ("status",)
STATE = ("pause", "unpause", "cycle", "flatten", "kill", "resume")
CONFIRMED = ("flatten", "kill", "resume")        # need an explicit yes
COMMANDS = READ_ONLY + STATE

DESCRIPTIONS = {
    "status": "mode, state, equity, positions with stops and targets, risk state, last cycle, monitor health",
    "pause": "stop new decision cycles (open positions keep their stops); repeating it is harmless",
    "unpause": "start decision cycles again; repeating it is harmless",
    "cycle": "run one decision cycle now (the TUI's C key)",
    "flatten": "close every open position at market; trading continues afterwards (needs --yes)",
    "kill": "close every position at market and HALT until resumed (needs --yes)",
    "resume": "lift a kill-switch halt and reset the drawdown baseline to current equity (needs --yes)",
}


# --------------------------------------------------------------------------- where the token lives
def control_enabled() -> bool:
    """AEON_CONTROL=0 / off / false / no turns the channel off entirely (no listener, no file)."""
    return os.environ.get("AEON_CONTROL", "").strip().lower() not in ("0", "off", "false", "no")


def control_path(mode: str) -> Path:
    """Next to the heartbeat file, so it lives wherever the container's private /tmp does."""
    env = os.environ.get("AEON_CONTROL_FILE")
    return Path(env) if env else heartbeat_path(mode).parent / f"aeon-{mode}.control"


def write_private(path: Path, text: str) -> None:
    """Create `path` with mode 0600 from the first instant (no window where it is readable by others), then move it
    into place atomically. On Windows the mode bits do nothing; the file inherits the per-user temp directory's ACL."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def private_problem(st, uid: int | None) -> str | None:
    """Why a token file must not be trusted, or None. Checked by the client on POSIX (`uid` is None elsewhere, where
    there are no mode bits to read): in a shared /tmp another user could plant a file that points the client at their
    own listener. root may read anyone's file, so for root only the mode is checked."""
    if not stat.S_ISREG(st.st_mode):                            # lstat: a symlink is not a regular file
        return "is not a regular file"
    if uid is None:
        return None
    if uid != 0 and st.st_uid != uid:
        return f"is owned by uid {st.st_uid}, not by you (uid {uid})"
    if st.st_mode & 0o077:
        return f"is readable by other users (mode {stat.S_IMODE(st.st_mode):04o}); expected 0600"
    return None


def read_endpoint(path: Path) -> tuple[int, str]:
    """(port, token) from the control file; ValueError with an operator-readable message when it is unusable."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        modes = sorted(p.name[5:-8] for p in path.parent.glob("aeon-*.control")) if path.name.startswith("aeon-") else []
        hint = f" A control file exists for mode {modes[0]}: add --mode {modes[0]}." if len(modes) == 1 else ""
        raise ValueError(f"no control file at {path}: is a headless AEON running in this mode, "
                         f"and is AEON_CONTROL not off?{hint}") from None
    except OSError as e:
        raise ValueError(f"cannot read the control file {path}: {e.strerror or e}") from None
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    if (why := private_problem(st, uid)):
        raise ValueError(f"refusing the control file {path}: it {why}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"cannot read the control file {path}: {e.strerror or e} "
                         "(run this as the user AEON runs as)") from None
    try:
        data = json.loads(raw)
        port, token = int(data["port"]), data["token"]
    except (ValueError, KeyError, TypeError):
        raise ValueError(f"the control file {path} is malformed") from None
    if not (0 < port < 65536) or not isinstance(token, str) or not token:
        raise ValueError(f"the control file {path} is malformed")
    return port, token


# --------------------------------------------------------------------------- the status snapshot
def _clean(v):
    """JSON-safe: NaN and infinity become null (the wire format is strict JSON)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def snapshot(agent, now: float | None = None) -> dict:
    """Everything an operator wants before deciding what to press. Read-only."""
    now = now or time.time()
    acct = agent.account
    positions = []
    if acct:
        for inst, p in acct.positions.items():
            positions.append(dict(inst=inst, name=agent._name(inst), side=p.side, kind=p.kind, contracts=p.contracts,
                                  entry=p.entry, mark=p.mark, leverage=p.lever, stop=p.sl, target=p.tp,
                                  upl=p.upl, liq=p.liq))
    risk = agent.risk.status(acct) if acct else {}
    risk.update(killed=agent.risk.killed, kill_reason=agent.risk.kill_reason, day_blocked=agent.risk.day_blocked)
    stalled = agent.stalled_for()
    last_cycle = agent.j.last_cycle_ts()
    return _clean(dict(
        mode=agent.mode,
        status=agent.status,                                   # RUNNING | PAUSED | HALTED (| STARTING)
        paused=agent.paused,
        thinking=agent.thinking,
        brain=f"{agent.brain.name} ({agent.brain.label})",
        uptime_s=now - agent.started,
        equity=acct.equity if acct else None,
        available=acct.available if acct else None,
        unrealized=acct.upl if acct else None,
        positions=positions,
        risk=risk,
        cycle_no=agent.cycle_no,                               # decisions made by this process
        last_cycle_ts=last_cycle,                              # the last booked decision, even from before a restart
        last_cycle_age_s=(now - last_cycle) if last_cycle else None,
        next_cycle_in_s=max(agent.next_cycle_ts - now, 0.0) if agent.next_cycle_ts else None,
        stalled_for_s=None if math.isinf(stalled) else stalled,
        monitor_alive=not math.isinf(stalled),                 # false = a background task has died
        monitor_error=agent.monitor_error,
        last_successful_monitor_ts=agent.last_successful_monitor,
    ))


# --------------------------------------------------------------------------- the server side
class Controller:
    """Turns a validated command into Agent calls. No network code in here."""

    def __init__(self, agent):
        self.agent = agent
        self._serial = asyncio.Lock()                          # state-changing commands run one at a time
        self._jobs: set[asyncio.Task] = set()                  # kept alive so a slow flatten outlives a timed-out client

    async def run(self, cmd: str, confirm) -> dict:
        if cmd == "status":
            return dict(ok=True, **snapshot(self.agent))
        if cmd in CONFIRMED and confirm is not True:
            return dict(ok=False, error=f"{cmd} needs explicit confirmation (send confirm: true / pass --yes)")
        async with self._serial:
            if cmd in CONFIRMED:
                self.agent.emit("risk", f"control channel: {cmd} requested")      # an audit line; Discord gets it too
            return await getattr(self, f"_do_{cmd}")()

    # pause / unpause are idempotent on purpose: a retried or doubled command must never flip the state back
    async def _do_pause(self) -> dict:
        if self.agent.paused:
            return dict(ok=True, message="already paused", status=self.agent.status)
        self.agent.toggle_pause()
        return dict(ok=True, message="decision cycles paused (open positions keep their stops)", status=self.agent.status)

    async def _do_unpause(self) -> dict:
        if not self.agent.paused:
            return dict(ok=True, message="not paused", status=self.agent.status)
        self.agent.toggle_pause()
        note = " (still HALTED: use resume)" if self.agent.risk.killed else ""
        return dict(ok=True, message="decision cycles resumed" + note, status=self.agent.status)

    async def _do_cycle(self) -> dict:
        if self.agent.risk.killed:
            return dict(ok=False, error="HALTED by the kill switch: no cycle will run until resume")
        if self.agent.thinking:
            return dict(ok=False, error="a decision cycle is already in progress")
        self.agent.cycle_now()
        return dict(ok=True, message="decision cycle requested"
                                     + (" (agent is paused; this one runs anyway)" if self.agent.paused else ""))

    async def _do_flatten(self) -> dict:
        before = self._open()
        if not await self._finish(self.agent.flatten("manual flatten (ctl)")):
            return self._still_working("flatten")
        left = self._open()
        if left:
            return dict(ok=False, positions_before=before, positions_after=left,
                        error=f"{left} position(s) still open after flatten (see the log)")
        return dict(ok=True, positions_before=before, positions_after=0,
                    message=f"{before} position(s) closed, none open; trading continues")

    async def _do_kill(self) -> dict:
        before = self._open()
        if not await self._finish(self.agent.kill("manual kill switch (ctl)")):
            return self._still_working("kill")
        left = self._open()
        r = dict(ok=True, status=self.agent.status, positions_before=before, positions_after=left)
        if left:                                                # the halt holds; the monitor keeps trying to close
            r["message"] = (f"HALTED, but {left} position(s) are still open: the monitor keeps retrying (see the log). "
                            "If it cannot, close them on OKX")
        else:
            r["message"] = f"HALTED: {before} position(s) closed, none open. Nothing trades until resume"
        return r

    async def _do_resume(self) -> dict:
        agent = self.agent
        # Exactly the TUI's R key: only when the kill switch is tripped. resume() also resets the drawdown baseline to
        # the current equity, so calling it on a healthy agent would quietly erase the real drawdown history.
        if not agent.risk.killed:
            return dict(ok=False, error="nothing to resume: the kill switch is not tripped (use unpause to un-pause)")
        if agent.account is None:
            return dict(ok=False, error="account not loaded yet; try again in a few seconds")
        agent.resume()
        return dict(ok=True, status=agent.status,
                    message=f"trading resumed; drawdown baseline reset to current equity {agent.account.equity:,.2f}")

    def _open(self) -> int:
        return len(self.agent.account.positions) if self.agent.account else 0

    async def _finish(self, coro) -> bool:
        """Run an exchange-touching coroutine to completion even if the client gives up or the timeout passes: a
        half-finished flatten is worse than a slow one. False = still running when the time was up."""
        job = asyncio.ensure_future(coro)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        try:
            await asyncio.wait_for(asyncio.shield(job), SLOW_TIMEOUT_S)
        except asyncio.TimeoutError:
            return False
        return True

    @staticmethod
    def _still_working(what: str) -> dict:
        return dict(ok=False, error=f"{what} is still working after {SLOW_TIMEOUT_S:.0f}s and carries on in the "
                                    "background; run status to see where it stands")


class ControlServer:
    """The loopback listener plus the token file. start() after the agent is up, stop() on the way out."""

    def __init__(self, agent, path: Path):
        self.controller, self.path = Controller(agent), path
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, HOST, 0, limit=MAX_REQUEST_BYTES)
        self.port = self._server.sockets[0].getsockname()[1]
        try:
            write_private(self.path, json.dumps({"port": self.port, "token": self.token}))
        except OSError:
            await self.stop()
            raise
        return self.port

    async def stop(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass
        if self._server:
            self._server.close()
            for w in list(self._writers):
                w.close()
            jobs = list(self.controller._jobs)
            if jobs:                                            # a flatten already under way finishes before we exit
                await asyncio.wait(jobs, timeout=JOB_GRACE_S)
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except Exception:
                pass
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._writers) >= MAX_CLIENTS:
            await self._reply(writer, dict(ok=False, error="busy: too many connections"))
            return
        self._writers.add(writer)
        try:
            await self._reply(writer, await self._serve(reader))
        except Exception as e:                                  # a bad client must never reach the agent's loop
            log(f"control: unexpected {type(e).__name__} while serving a request")
            await self._reply(writer, dict(ok=False, error="internal error"))
        finally:
            self._writers.discard(writer)

    async def _serve(self, reader: asyncio.StreamReader) -> dict:
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\n"), READ_TIMEOUT_S)
        except asyncio.TimeoutError:
            return dict(ok=False, error="timed out waiting for the request")
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            return dict(ok=False, error=f"request must be one line of at most {MAX_REQUEST_BYTES} bytes")
        try:
            req = json.loads(line)
        except ValueError:
            return dict(ok=False, error="bad request: not JSON")
        if not isinstance(req, dict):
            return dict(ok=False, error="bad request: expected a JSON object")
        if not self._authorised(req.get("token")):
            log("control: refused a request with a missing or wrong token")
            return dict(ok=False, error="unauthorized")
        cmd = req.get("cmd")
        if cmd not in COMMANDS:                                  # a tuple lookup: a list or dict here is simply not found
            return dict(ok=False, error=f"unknown command; use one of: {', '.join(COMMANDS)}")
        return await self.controller.run(cmd, req.get("confirm"))

    def _authorised(self, supplied) -> bool:
        if not isinstance(supplied, str):
            return False
        return hmac.compare_digest(supplied.encode("utf-8", "replace"), self.token.encode("ascii"))

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, payload: dict) -> None:
        try:
            writer.write(json.dumps(payload, allow_nan=False).encode("utf-8") + b"\n")
            await asyncio.wait_for(writer.drain(), READ_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError, ValueError):
            pass                                                # the client went away; the work was already done
        finally:
            writer.close()


def log(msg: str) -> None:
    print(f"[aeon] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- the client side (`--ctl`)
async def request(path: Path, cmd: str, confirm: bool = False) -> dict:
    """Send one command to the running agent and return its reply. ValueError for anything the operator can fix."""
    port, token = read_endpoint(path)
    wait = (SLOW_TIMEOUT_S if cmd in ("flatten", "kill") else READ_TIMEOUT_S * 2) + CLIENT_PAD_S
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, port, limit=MAX_REPLY_BYTES),
                                                CONNECT_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError):
        raise ValueError(f"cannot connect to AEON on {HOST}:{port} (stale control file {path}? the agent may have "
                         "stopped or be restarting)") from None
    try:
        writer.write(json.dumps({"token": token, "cmd": cmd, "confirm": confirm}).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), wait)
        reply = json.loads(line)
    except asyncio.TimeoutError:
        raise ValueError(f"no reply within {wait:.0f}s; the command may still be running: check status") from None
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
        raise ValueError("the control channel closed the connection or sent an unreadable reply") from None
    finally:
        writer.close()
    if not isinstance(reply, dict):
        raise ValueError("the control channel sent an unreadable reply")
    return reply


def _dur(seconds) -> str:
    s = int(seconds)
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def _ago(seconds) -> str:
    return "never" if seconds is None else f"{_dur(seconds)} ago"


def _num(v, spec=",.2f") -> str:
    return "-" if v is None else format(v, spec)


def format_status(r: dict) -> str:
    risk = r.get("risk") or {}
    lines = [f"AEON {r['mode'].upper()} | {r['status']} | brain {r['brain']} | up {_dur(r['uptime_s'])}",
             f"equity {_num(r['equity'])} USDT | available {_num(r['available'])} | unrealized {_num(r['unrealized'], '+,.2f')}"]
    if risk.get("killed"):
        lines.append(f"HALTED: {risk.get('kill_reason') or 'kill switch'}  ->  --ctl resume --yes, once you have reviewed")
    if risk.get("day_blocked"):
        lines.append("daily loss limit hit: no new positions until 00:00 UTC")
    if r["paused"] and not risk.get("killed"):
        lines.append("PAUSED: no new decisions (open positions keep their stops)  ->  --ctl unpause")
    if risk.get("dd_limit") is not None:
        lines.append(f"risk: drawdown {_num(risk.get('drawdown'), '.1f')}% of {_num(risk.get('dd_limit'), '.0f')}% | "
                     f"today {_num(risk.get('day_pnl'), '+.1f')}% (limit -{_num(risk.get('day_limit'), '.0f')}%) | "
                     f"exposure {_num(risk.get('exposure'), '.2f')}x of {_num(risk.get('exposure_limit'), '.1f')}x | "
                     f"trades today {risk.get('trades_today')}/{risk.get('trades_limit')}")
    pos = r["positions"]
    lines.append(f"positions: {len(pos)}" + ("" if pos else " (flat)"))
    for p in pos:
        lines.append(f"  {p['name']:<10} {p['side']:<5} {_num(p['contracts'], 'g')} @ {_num(p['entry'], 'g')}  "
                     f"mark {_num(p['mark'], 'g')}  stop {_num(p['stop'], 'g')}  target {_num(p['target'], 'g')}  "
                     f"upl {_num(p['upl'], '+,.2f')}")
    nxt = "-" if r["next_cycle_in_s"] is None else _dur(r["next_cycle_in_s"])
    lines.append(f"cycles: {r['cycle_no']} this run | last decision {_ago(r['last_cycle_age_s'])} | next in {nxt}"
                 + (" | thinking now" if r["thinking"] else ""))
    if r["monitor_alive"]:
        lines.append(f"monitor: checked in {_ago(r['stalled_for_s'])}")
    else:
        lines.append("monitor: A BACKGROUND TASK HAS DIED; the watchdog restarts the process")
    return "\n".join(lines)


def format_reply(cmd: str, reply: dict) -> tuple[str, int]:
    """(text, exit code) for the operator: 0 done, 1 refused or failed."""
    if not reply.get("ok"):
        return f"{cmd}: {reply.get('error', 'failed')}", 1
    if cmd == "status":
        return format_status(reply), 0
    return f"{cmd}: {reply.get('message', 'done')}" + (f" [{reply['status']}]" if reply.get("status") else ""), 0


def run_ctl(mode: str, cmd: str, confirmed: bool) -> int:
    """`python -m aeon --ctl <cmd> [--yes]`. Exit 0 done, 1 refused by the agent, 2 could not talk to it."""
    if cmd in CONFIRMED and not confirmed:
        print(f"{cmd}: {DESCRIPTIONS[cmd]}.\nAdd --yes to confirm.", file=sys.stderr)
        return 2
    try:
        reply = asyncio.run(request(control_path(mode), cmd, confirm=confirmed))
    except ValueError as e:
        print(f"ctl: {e}", file=sys.stderr)
        return 2
    text, code = format_reply(cmd, reply)
    print(text, file=sys.stdout if code == 0 else sys.stderr)
    return code
