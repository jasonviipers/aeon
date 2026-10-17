"""Live price feed: OKX public WebSocket (tickers), falling back to REST polling."""
from __future__ import annotations

import asyncio
import json
import time

import websockets

from .okx import WS_PUBLIC_URL, OkxClient, OkxError


class PriceFeed:
    def __init__(self, okx: OkxClient):
        self.okx = okx
        self.prices: dict[str, float] = {}
        self.updated: float = 0.0           # wall time of the last tick from any instrument
        self.transport = "init"             # init | ws | rest | down
        self._subs: set[str] = set()
        self._resub = asyncio.Event()

    def watch(self, insts: list[str] | set[str]) -> None:
        new = set(insts)
        if new != self._subs:
            self._subs = new
            self._resub.set()

    def age(self) -> float:
        return time.time() - self.updated if self.updated else float("inf")

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
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            for row in msg.get("data", []) if isinstance(msg, dict) else []:
                try:
                    self.prices[row["instId"]] = float(row["last"])
                    self.updated = time.time()
                except (KeyError, ValueError, TypeError):
                    continue

    async def _rest_poll(self) -> None:
        try:
            for t in await self.okx.tickers("SWAP"):
                if t.get("instId") in self._subs:
                    self.prices[t["instId"]] = float(t["last"])
                    self.updated = time.time()
        except (OkxError, ValueError):
            self.transport = "down"
