"""Minimal async OKX v5 REST client: public market data + signed trading calls.

Only what AEON needs. Orders are never retried automatically (a retried POST could
double-fill); every order carries a unique clOrdId so it can be looked up afterwards.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import httpx

BASE_URL = "https://www.okx.com"
WS_PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"


class OkxError(Exception):
    def __init__(self, code: str, msg: str, path: str = ""):
        super().__init__(f"OKX {code}: {msg} ({path})" if path else f"OKX {code}: {msg}")
        self.code, self.msg, self.path = code, msg, path


def fmt(x: float | str) -> str:
    """Plain decimal string with no exponent or trailing zeros."""
    return format(Decimal(str(x)).normalize(), "f")


def round_step(x: float, step: float) -> float:
    s = Decimal(str(step))
    return float((Decimal(str(x)) / s).to_integral_value(ROUND_HALF_UP) * s)


def px_str(x: float, tick: float) -> str:
    return fmt(round_step(x, tick))


class OkxClient:
    def __init__(self, key: str = "", secret: str = "", passphrase: str = "",
                 demo: bool = False, timeout: float = 15.0):
        self.key, self.secret, self.passphrase, self.demo = key, secret, passphrase, demo
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- transport -----------------------------------------------------------
    def _sign(self, ts: str, method: str, path_qs: str, body: str) -> str:
        mac = hmac.new(self.secret.encode(), f"{ts}{method}{path_qs}{body}".encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    async def request(self, method: str, path: str, params: dict | None = None,
                      body: dict | list | None = None, auth: bool = False, retries: int = 2) -> list:
        qs = ""
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                qs = "?" + urlencode(clean)
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        attempt = 0
        while True:
            headers = {"Content-Type": "application/json"}
            if auth:
                if not (self.key and self.secret and self.passphrase):
                    raise OkxError("auth", "OKX API credentials are not configured", path)
                ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                headers.update({
                    "OK-ACCESS-KEY": self.key,
                    "OK-ACCESS-SIGN": self._sign(ts, method, path + qs, payload),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                })
                if self.demo:
                    headers["x-simulated-trading"] = "1"
            try:
                resp = await self._http.request(method, path + qs, content=payload or None, headers=headers)
            except httpx.TransportError as e:
                if method == "GET" and attempt < retries:
                    attempt += 1
                    await asyncio.sleep(0.6 * attempt)
                    continue
                raise OkxError("net", f"{type(e).__name__}: {e}", path) from e
            if resp.status_code in (429, 500, 502, 503) and method == "GET" and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.8 * attempt)
                continue
            if method != "GET" and resp.status_code >= 500:
                # A gateway or server error on a write says nothing about whether the write happened. Report it the
                # way a lost reply is reported ("net"), so the broker looks the order up instead of assuming it failed.
                raise OkxError("net", f"HTTP {resp.status_code} on a write: outcome unknown", path)
            try:
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
            except ValueError:
                if method != "GET" and resp.status_code < 400:
                    raise OkxError("net", f"HTTP {resp.status_code} with an unreadable reply: outcome unknown", path) from None
                raise OkxError(str(resp.status_code), resp.text[:200], path) from None
            if data.get("code") != "0":
                detail = ""
                rows = data.get("data") or []
                if rows and isinstance(rows[0], dict) and rows[0].get("sMsg"):
                    detail = f" [{rows[0].get('sCode')}] {rows[0]['sMsg']}"
                raise OkxError(str(data.get("code")), (data.get("msg") or "") + detail, path)
            return data.get("data", [])

    async def get(self, path: str, params: dict | None = None, auth: bool = False) -> list:
        return await self.request("GET", path, params=params, auth=auth)

    async def post(self, path: str, body: dict | list, auth: bool = True) -> list:
        return await self.request("POST", path, body=body, auth=auth, retries=0)

    # --- public market data --------------------------------------------------
    async def server_time(self) -> float:
        """OKX's clock in unix seconds. Signed requests are refused when this machine's clock is off by too much."""
        rows = await self.get("/api/v5/public/time")
        return float(rows[0]["ts"]) / 1000.0

    async def instruments(self, inst_type: str = "SWAP", family: str | None = None) -> list[dict]:
        """family (e.g. "BTC-USD") is required by OKX for OPTION and optional for the other types."""
        return await self.get("/api/v5/public/instruments", {"instType": inst_type, "instFamily": family})

    async def tickers(self, inst_type: str = "SWAP") -> list[dict]:
        return await self.get("/api/v5/market/tickers", {"instType": inst_type})

    async def candles(self, inst: str, bar: str = "15m", limit: int = 100) -> list[list[str]]:
        """Newest first: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]."""
        return await self.get("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": limit})

    async def funding_rate(self, inst: str) -> dict | None:
        rows = await self.get("/api/v5/public/funding-rate", {"instId": inst})
        return rows[0] if rows else None

    async def index_tickers(self, quote: str = "USDT") -> dict[str, float]:
        """Spot index prices for every coin quoted in `quote`, in one call: {"BTC-USDT": 81640.1, ...}."""
        rows = await self.get("/api/v5/market/index-tickers", {"quoteCcy": quote})
        out: dict[str, float] = {}
        for r in rows:
            try:
                out[r["instId"]] = float(r["idxPx"])
            except (KeyError, ValueError, TypeError):
                continue
        return out

    # --- account -------------------------------------------------------------
    async def account_config(self) -> list[dict]:
        return await self.get("/api/v5/account/config", auth=True)

    async def balance(self) -> list[dict]:
        return await self.get("/api/v5/account/balance", auth=True)

    async def positions(self, inst_type: str = "SWAP") -> list[dict]:
        return await self.get("/api/v5/account/positions", {"instType": inst_type}, auth=True)

    async def positions_history(self, inst: str, limit: int = 3, inst_type: str = "SWAP") -> list[dict]:
        return await self.get("/api/v5/account/positions-history",
                              {"instType": inst_type, "instId": inst, "limit": limit}, auth=True)

    async def fills(self, inst_type: str, inst: str, limit: int = 30) -> list[dict]:
        """Recent executions (newest first): side, fillPx, fillSz, fee (negative), feeCcy, ts."""
        return await self.get("/api/v5/trade/fills", {"instType": inst_type, "instId": inst, "limit": limit}, auth=True)

    async def set_leverage(self, inst: str, lever: float, mgn_mode: str, pos_side: str | None) -> list[dict]:
        body = {"instId": inst, "lever": fmt(lever), "mgnMode": mgn_mode}
        if pos_side:
            body["posSide"] = pos_side
        return await self.post("/api/v5/account/set-leverage", body)

    # --- trading -------------------------------------------------------------
    async def place_order(self, body: dict) -> list[dict]:
        rows = await self.post("/api/v5/trade/order", body)
        if rows and rows[0].get("sCode") not in (None, "0"):
            raise OkxError(rows[0]["sCode"], rows[0].get("sMsg", "order rejected"), "/api/v5/trade/order")
        return rows

    async def get_order(self, inst: str, ord_id: str | None = None, cl_id: str | None = None) -> dict | None:
        """One order, by OKX's id or by the client id AEON gave it before sending (so a lost reply is recoverable)."""
        params = {"instId": inst, "ordId": ord_id} if ord_id else {"instId": inst, "clOrdId": cl_id}
        rows = await self.get("/api/v5/trade/order", params, auth=True)
        return rows[0] if rows else None

    async def algo_details(self, algo_id: str) -> dict | None:
        """One stop / take-profit order, also after it fired: state (live, effective, canceled, order_failed, ...),
        ordIdList (the order it placed when it fired) and actualSide ("sl" or "tp")."""
        rows = await self.get("/api/v5/trade/order-algo", {"algoId": algo_id}, auth=True)
        return rows[0] if rows else None

    async def close_position(self, body: dict) -> list[dict]:
        return await self.post("/api/v5/trade/close-position", body)

    async def algos_pending(self, ord_type: str, inst_type: str | None = None) -> list[dict]:
        """Pending stop / take-profit orders. Without inst_type OKX returns every product type at once."""
        return await self.get("/api/v5/trade/orders-algo-pending",
                              {"ordType": ord_type, "instType": inst_type}, auth=True)

    async def place_algo(self, body: dict) -> list[dict]:
        rows = await self.post("/api/v5/trade/order-algo", body)
        if rows and rows[0].get("sCode") not in (None, "0"):
            raise OkxError(rows[0]["sCode"], rows[0].get("sMsg", "algo rejected"), "/api/v5/trade/order-algo")
        return rows

    async def cancel_algos(self, items: list[dict]) -> list[dict]:
        return await self.post("/api/v5/trade/cancel-algos", items)
