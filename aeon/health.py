"""Liveness for an unattended process.

Three small, independent pieces:
  * a heartbeat file the monitor loop touches on every pass, read by `python -m aeon --healthcheck` (the Docker
    HEALTHCHECK) so `docker ps` shows a wedged agent as unhealthy;
  * a watchdog thread that ends the process if the monitor stalls. Docker restarts a container that EXITS but
    never one that is merely "unhealthy", so this is what actually recovers a hung agent. The thread is separate
    from the event loop on purpose: it still runs when the loop is blocked. Exchange-side stops keep protecting
    open positions while the process restarts, and unconfirmed orders are saved before they are sent;
  * an optional ping to an external "dead man's switch" URL (healthchecks.io, Better Stack, UptimeRobot...). It is
    the only way to hear about the whole VPS going down: Discord alerts die with the process.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

HEALTHY_WITHIN_S = 180.0        # healthcheck: the heartbeat must be younger than this
WATCHDOG_S = 600.0              # watchdog: exit when the monitor has been silent this long (0 = off)
PING_EVERY_S = 300.0
EXIT_CODE = 70                  # sysexits EX_SOFTWARE: "the watchdog ended this process"


def heartbeat_path(mode: str) -> Path:
    env = os.environ.get("AEON_HEARTBEAT_FILE")
    return Path(env) if env else Path(tempfile.gettempdir()) / f"aeon-{mode}.heartbeat"


def write_heartbeat(path: Path, ts: float | None = None, **info) -> None:
    """Atomic: a reader never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"ts": ts or time.time(), **info}), encoding="utf-8")
    os.replace(tmp, path)


def check_heartbeat(path: Path, max_age: float = HEALTHY_WITHIN_S, now: float | None = None) -> tuple[bool, str]:
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        age = (now or time.time()) - float(beat["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return False, f"no readable heartbeat at {path}"
    if age > max_age:
        return False, f"monitor silent for {age:.0f}s (limit {max_age:.0f}s)"
    return True, f"monitor alive {age:.0f}s ago"


def watchdog_seconds() -> float:
    raw = os.environ.get("AEON_WATCHDOG_S", "")
    try:
        return max(float(raw), 0.0) if raw.strip() else WATCHDOG_S
    except ValueError:
        return WATCHDOG_S


def _die(silent_for: float) -> None:
    print(f"[aeon] watchdog: no monitor heartbeat for {silent_for:.0f}s; exiting so the supervisor restarts the agent. "
          "Open positions keep their exchange-side stops.", file=sys.stderr, flush=True)
    os._exit(EXIT_CODE)


class Watchdog(threading.Thread):
    """Calls on_trip(silent_for) once, if silent_for() ever exceeds `limit` seconds."""

    def __init__(self, silent_for, limit: float, on_trip=_die, poll: float = 5.0):
        super().__init__(name="aeon-watchdog", daemon=True)
        self.silent_for, self.limit, self.on_trip, self.poll = silent_for, limit, on_trip, poll
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.wait(self.poll):
            age = self.silent_for()
            if age > self.limit:
                self.on_trip(age)
                return

    def stop(self) -> None:
        self._stop_evt.set()


def ping_url() -> str:
    """The dead man's switch URL from HEARTBEAT_URL, or '' when unset or not https."""
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    return url if url.startswith("https://") else ""


async def ping_loop(url: str, healthy, every: float = PING_EVERY_S, client: httpx.AsyncClient | None = None) -> None:
    """GET `url` every `every` seconds while healthy() is true. No payload, nothing about the account leaves the
    box. When AEON stalls or the VPS dies the pings stop and the monitoring service alerts you."""
    import asyncio
    http = client or httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "AEON/0.1 (+heartbeat)"})
    try:
        while True:
            if healthy():
                try:
                    await http.get(url)
                except httpx.HTTPError:
                    pass                                 # best effort: the next ping tries again
            await asyncio.sleep(every)
    finally:
        if client is None:
            await http.aclose()
