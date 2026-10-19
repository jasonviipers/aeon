"""Live price feed: OKX public WebSocket (tickers), falling back to REST polling.

Every price carries its own timestamp, so a busy perpetual can never make a quiet spot pair or dated
future look fresh, and the REST fallback covers all three market kinds, not just perpetuals."""
from __future__ import annotations

import asyncio
import json
import time

import websockets

from .okx import WS_PUBLIC_URL, OkxClient, OkxError


def inst_type_of(inst: str) -> str:
    """OKX instrument type from the id: SOL-USDT-SWAP -> SWAP, SOL-USDT -> SPOT, BTC-USD_UM-261030 -> FUTURES."""
    if inst.endswith("-SWAP"):
        return "SWAP"
    return "SPOT" if inst.count("-") == 1 else "FUTURES"


class PriceFeed:
    def __init__(self, okx: OkxClient):
        self.okx = okx
        self.prices: dict[str, float] = {}
        self.ts: dict[str, float] = {}      # per-instrument time of its last price
        self.updated: float = 0.0           # wall time of the last tick from any instrument (connection health)
        self.transport = "init"             # init | ws | rest | down
        self._subs: set[str] = set()
        self._resub = asyncio.Event()

    def watch(self, insts: list[str] | set[str]) -> None:
        new = set(insts)
        if new != self._subs:
            self._subs = new
            self._resub.set()

    def set(self, inst: str, price: float, ts: float | None = None) -> None:
        now = time.time() if ts is None else ts
        self.prices[inst] = price
        self.ts[inst] = now
        self.updated = max(self.updated, now)

    def age(self, inst: str | None = None) -> float:
        """Seconds since the last price: for one instrument, or (no argument) for the feed as a whole."""
        t = self.ts.get(inst) if inst is not None else self.updated
        return time.time() - t if t else float("inf")

    async def refresh(self, insts: list[str] | set[str]) -> None:
        """Fetch current prices for these instruments over REST, whatever their market kind."""
        wanted = set(insts)
        for inst_type in sorted({inst_type_of(i) for i in wanted}):
            started = time.time()
            for row in await self.okx.tickers(inst_type):
                if row.get("instId") in wanted:
                    try:
                        timestamp = float(row.get("ts") or started * 1000.0) / 1000.0
                        self.set(row["instId"], float(row["last"]), timestamp)
                    except (KeyError, ValueError, TypeError):
                        continue

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._ws_session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                self.transport = "rest"
                await self._rest_poll()
                await asyncio.sleep(min(backoff, 20.0))
                backoff = min(backoff * 2, 20.0)

    async def _ws_session(self) -> None:
        self._resub.clear()
        subs = sorted(self._subs)
        if not subs:
            await self._resub.wait()
            return
        async with websockets.connect(WS_PUBLIC_URL, ping_interval=20, ping_timeout=20,
                                      open_timeout=10, max_queue=256) as ws:
            await ws.send(json.dumps({"op": "subscribe",
                                      "args": [{"channel": "tickers", "instId": i} for i in subs]}))
            self.transport = "ws"
            reader = asyncio.create_task(self._read(ws))
            waiter = asyncio.create_task(self._resub.wait())
            try:
                done, _ = await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (reader, waiter):
                    task.cancel()
                results = await asyncio.gather(reader, waiter, return_exceptions=True)
            if reader in done and isinstance(results[0], Exception) and not isinstance(results[0], asyncio.CancelledError):
                raise results[0]   # a dropped connection: run() falls back to REST and retries

    async def _read(self, ws) -> None:
        async for raw in ws:
            received = time.time()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            for row in msg.get("data", []) if isinstance(msg, dict) else []:
                try:
                    timestamp = float(row.get("ts") or received * 1000.0) / 1000.0
                    self.set(row["instId"], float(row["last"]), timestamp)
                except (KeyError, ValueError, TypeError):
                    continue

    async def _rest_poll(self) -> None:
        try:
            await self.refresh(self._subs)
        except (OkxError, ValueError, AttributeError):
            self.transport = "down"
