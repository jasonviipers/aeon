"""Discord alerts: the agent's events, rendered as rich embeds and posted to a webhook.

Rules that keep this safe to bolt onto a trader:
  * a send may never slow down or break the trading loop: posts are queued and delivered by a
    background task, best-effort, with 429/5xx retries;
  * mentions are OFF unless the event is a halt, and model-written text is never allowed to
    mention anyone, so a reason like "@everyone" cannot ping your server;
  * identical messages inside notify.cooldown_s are dropped, and at most notify.max_per_minute
    leave the process per minute, so a flapping exchange cannot spam the channel.

Enable it in config.toml ([notify] enabled = true) and put the webhook URL in .env.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from .config import NotifyCfg

# Embed colours (Discord's palette): green = good, red = bad, amber = the governor had a say.
GREEN = 0x3BA55D
RED = 0xED4245
AMBER = 0xFAA61A
BLURPLE = 0x5865F2
GREY = 0x99AAB5

WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/",
                    "https://canary.discord.com/api/webhooks/", "https://ptb.discord.com/api/webhooks/")

MAX_TITLE, MAX_DESC, MAX_VALUE, MAX_FOOTER, MAX_FIELDS = 256, 4096, 1024, 2048, 25


# --------------------------------------------------------------------------- formatting
def _cut(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _px(x) -> str:
    """Prices with a sensible number of decimals (81,644.8 / 104.90 / 1.3050 / 0.080250)."""
    if x is None:
        return "-"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    a = abs(x)
    d = 1 if a >= 1000 else 2 if a >= 10 else 4 if a >= 1 else 6
    return f"{x:.{d}f}"


def _money(x, dp: int = 2, sign: bool = False) -> str:
    if x is None:
        return ""
    return f"{x:+.{dp}f}" if sign else f"{x:.{dp}f}"


def _level(price, level) -> str:
    """A stop or target price, with its distance from the current price."""
    if level in (None, ""):
        return ""
    if not price:
        return _px(level)
    return f"{_px(level)} ({(float(level) - float(price)) / float(price) * 100:+.2f}%)"


def _field(name: str, value: str) -> dict:
    return {"name": _cut(name, MAX_TITLE), "value": _cut(value, MAX_VALUE) or "-", "inline": True}


def _embed(title: str | None, description: str | None, color: int, fields: list[dict],
           ts: float, footer: str) -> dict:
    e: dict = {"color": color, "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat()}
    if title:
        e["title"] = _cut(title, MAX_TITLE)
    if description:
        e["description"] = _cut(description, MAX_DESC)
    if fields:
        e["fields"] = fields[:MAX_FIELDS]
    if footer:
        e["footer"] = {"text": footer}
    return e


def _fill_embed(text: str, meta: dict, ts: float, footer: str) -> dict:
    event = str(meta.get("event") or "fill")
    inst = str(meta.get("inst") or "")
    side = str(meta.get("side") or "").upper()
    spec_kind = str(meta.get("spec_kind") or "")
    unit = "coins" if spec_kind == "spot" else "ct"
    price, contracts, fee = meta.get("price"), meta.get("contracts"), meta.get("fee")
    pnl, trade_pnl = meta.get("pnl"), meta.get("trade_pnl")
    reason = str(meta.get("reason") or "")
    est = " (est.)" if meta.get("est") else ""
    liquidated = "liquidat" in reason.lower() or "liquidat" in text.lower()
    fields: list[dict] = []

    def add(name: str, value: str | None) -> None:
        if value:
            fields.append(_field(name, value))

    if event in ("open", "add"):
        lev = meta.get("leverage")
        tag = "spot" if spec_kind == "spot" else (f"{lev:g}x" if lev else "")
        title = f"{event.upper()} {side}{' ' + tag if tag else ''} · {inst}"
        add("Price", _px(price))
        size = f"{contracts:g} {unit}" if contracts is not None else ""
        if meta.get("notional"):
            size += f" · ${meta['notional']:,.0f}"
        add("Size", size)
        add("Stop", _level(price, meta.get("sl")))
        add("Target", _level(price, meta.get("tp")))
        if meta.get("max_loss") is not None:
            add("Risk at stop", f"${_money(meta['max_loss'])}")
        add("Fee", f"${_money(fee, 3)}" if fee is not None else "")
        return _embed(title, reason or None, GREEN, fields, ts, footer)

    win = (trade_pnl if trade_pnl is not None else pnl) or 0.0
    title = f"{'LIQUIDATED' if liquidated else event.upper()} {side} · {inst}".replace("  ", " ")
    color = RED if liquidated else (GREEN if win >= 0 else RED)
    add("Price", _px(price))
    add("Size", f"{contracts:g} {unit}" if contracts is not None else "")
    add("PnL", f"{_money(pnl, 2, True)}{est}" if pnl is not None else "")
    if trade_pnl is not None:
        add("Whole trade", f"{_money(trade_pnl, 2, True)}{est}")
    add("Fee", f"${_money(fee, 3)}" if fee is not None else "")
    desc = reason or ("closed on the exchange" if event == "close" else None)
    return _embed(title, desc, color, fields, ts, footer)


def _risk_embed(text: str, ts: float, footer: str) -> dict:
    low = text.lower()
    if "kill switch" in low or low.startswith(("halted", "kill switch")):
        title, color = "Kill switch / halt", RED
    elif "daily loss" in low:
        title, color = "Daily loss limit", AMBER
    elif "unprotected" in low:
        title, color = "Position unprotected", RED
    elif text.startswith("REJECTED"):
        title, color = "Rejected by the risk governor", AMBER
    elif "expires in" in low:
        title, color = "Dated future nearing expiry", AMBER
    elif low.startswith("adjusted"):
        title, color = "Risk governor adjusted the order", GREY
    elif "stale" in low:
        title, color = "Price feed stale", AMBER
    else:
        title, color = "Risk governor", AMBER
    return _embed(title, text, color, [], ts, footer)


def _error_embed(text: str, ts: float, footer: str) -> dict:
    low = text.lower()
    if "brain failed" in low:
        title = "Brain failure"
    elif "cycle crashed" in low:
        title = "Cycle crashed"
    elif low.startswith("monitor:"):
        title = "Monitor error"
    elif "before expiry" in low:
        title = "Expiry close failed"
    elif " failed" in low:
        title = "Order failed"
    elif "discord" in low:
        title = "Alerts"
    else:
        title = "Error"
    return _embed(title, text, RED, [], ts, footer)


def _action_embed(text: str, ts: float, footer: str) -> dict:
    head, _, tail = text.partition(" | ")
    return _embed(head or "Action", tail or None, BLURPLE, [], ts, footer)


def _startup_embed(text: str, meta: dict, ts: float, footer: str) -> dict:
    fields = []
    if meta.get("mode"):
        fields.append(_field("Mode", str(meta["mode"]).upper()))
    if meta.get("equity") is not None:
        fields.append(_field("Equity", f"${_money(meta['equity'])} USDT"))
    if meta.get("brain"):
        fields.append(_field("Brain", str(meta["brain"])))
    if meta.get("indexed"):
        fields.append(_field("Indexed", str(meta["indexed"])))
    return _embed("AEON online", None if fields else text, BLURPLE, fields, ts, footer)


def _system_embed(text: str, ts: float, footer: str) -> dict:
    if len(text) <= 120:
        return _embed(text, None, BLURPLE, [], ts, footer)
    return _embed("System", text, BLURPLE, [], ts, footer)


def build_embed(kind: str, text: str, meta: dict | None, ts: float, footer: str) -> dict:
    """One event -> one Discord embed. Never raises: unknown shapes fall back to the raw text."""
    meta = meta or {}
    try:
        if kind == "fill":
            return _fill_embed(text, meta, ts, footer)
        if kind == "risk":
            return _risk_embed(text, ts, footer)
        if kind == "error":
            return _error_embed(text, ts, footer)
        if kind == "action":
            return _action_embed(text, ts, footer)
        if kind == "system" and meta.get("startup"):
            return _startup_embed(text, meta, ts, footer)
        if kind == "system":
            return _system_embed(text, ts, footer)
    except Exception:
        pass
    return _embed(_cut(kind, 40) or "AEON", text, GREY, [], ts, footer)


def _is_halt(kind: str, text: str) -> bool:
    """Only a halt may ping: that is the one message that needs a human now."""
    low = text.lower()
    return kind == "risk" and ("kill switch" in low or low.startswith("halted"))


# --------------------------------------------------------------------------- notifier
class DiscordNotifier:
    """Queues events and posts them to a Discord webhook. Safe to call from sync code."""

    def __init__(self, cfg: NotifyCfg, mode: str = "", label: str = "",
                 client: httpx.AsyncClient | None = None, env: dict | None = None):
        self.cfg, self.mode, self.label = cfg, mode, label
        self.problem = ""
        env = os.environ if env is None else env
        self.url = (env.get(cfg.webhook_env) or "").strip()
        self.enabled = False
        if cfg.enabled and not self.url:
            self.problem = (f"Discord alerts are on in config.toml but {cfg.webhook_env} is empty in .env: "
                            "nothing will be posted")
        elif cfg.enabled and not self.url.startswith(WEBHOOK_PREFIXES):
            self.problem = f"{cfg.webhook_env} is not a Discord webhook URL: nothing will be posted"
        elif cfg.enabled:
            self.enabled = True
        parts = ["AEON"] + ([mode.upper()] if mode else []) + ([label] if label else [])
        self.footer = _cut(" · ".join(parts), MAX_FOOTER)
        self.throttle = 0.35                        # seconds between two posts (Discord asks ~5/s)
        self.sent = 0
        self.dropped = 0
        self._q: deque[tuple[str, str, dict, float]] = deque(maxlen=200)
        self._seen: dict[tuple[str, str], float] = {}
        self._recent: deque[float] = deque()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._busy = False
        self._http = client
        self._own_http = client is None

    # --- producing -----------------------------------------------------------
    def post(self, kind: str, text: str, meta: dict | None = None) -> None:
        """Queue one event. Filtering, dedupe and rate limiting happen here, cheaply and safely."""
        if not self.enabled or not text:
            return
        meta = meta or {}
        if kind not in self.cfg.events and not (kind == "system" and meta.get("startup")):
            return
        now = time.monotonic()
        key = (kind, text)
        if self.cfg.cooldown_s > 0:
            seen = self._seen.get(key)
            if seen is not None and now - seen < self.cfg.cooldown_s:
                self.dropped += 1
                return
            if len(self._seen) > 400:               # keep the dedupe table from growing forever
                self._seen = {k: v for k, v in self._seen.items() if now - v < self.cfg.cooldown_s}
        if self.cfg.max_per_minute > 0:
            while self._recent and now - self._recent[0] > 60.0:
                self._recent.popleft()
            if len(self._recent) >= self.cfg.max_per_minute:
                self.dropped += 1
                return
            self._recent.append(now)
        self._seen[key] = now
        self._q.append((kind, text, meta, time.time()))
        self._wake.set()

    def start(self) -> None:
        """Begin delivering in the background. Call once from inside the agent's loop."""
        if not self.enabled or (self._task and not self._task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run())

    # --- delivering ----------------------------------------------------------
    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self._process()

    async def _process(self) -> None:
        while self._q:
            kind, text, meta, ts = self._q.popleft()
            self._busy = True
            try:
                await self._send(build_embed(kind, text, meta, ts, self.footer),
                                 self.cfg.ping_on_halt and _is_halt(kind, text))
            except asyncio.CancelledError:
                self._busy = False
                raise
            except Exception:
                self.dropped += 1                   # alerts are best-effort: never surface an error
            finally:
                self._busy = False
            if self._q and self.throttle:
                await asyncio.sleep(self.throttle)

    async def _send(self, embed: dict, ping: bool) -> None:
        body: dict = {"embeds": [embed], "allowed_mentions": {"parse": ["everyone"] if ping else []}}
        if ping:
            body["content"] = "@here"
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "AEON/0.1 (+discord-alerts)"})
        last = "no response"
        for attempt in range(3):
            resp = await self._http.post(self.url, json=body)
            if resp.status_code == 429:
                wait = 1.0
                try:
                    wait = float(resp.json().get("retry_after") or wait)
                except Exception:
                    pass
                last = "429 (rate limited)"
                await asyncio.sleep(min(max(wait, 0.2), 5.0))
                continue
            if resp.status_code >= 500:
                last = f"{resp.status_code}"
                await asyncio.sleep(1.0 + attempt)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Discord {resp.status_code}: {resp.text[:120]}")
            self.sent += 1
            return
        raise RuntimeError(f"Discord unavailable after 3 tries ({last})")

    async def test(self) -> None:
        """Send one message now and raise if it fails (used by --check)."""
        embed = _embed("AEON online", "Discord alerts are wired up.", BLURPLE, [], time.time(), self.footer)
        await self._send(embed, ping=False)

    # --- lifecycle -----------------------------------------------------------
    async def flush(self, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while (self._q or self._busy) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        if self._q and (self._task is None or self._task.done()):
            await self._process()                   # nothing running (e.g. --once): deliver now
        else:
            await self.flush()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None
