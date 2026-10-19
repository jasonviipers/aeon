"""SQLite journal: everything the agent sees, decides and does is recorded here."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity (ts REAL NOT NULL, equity REAL NOT NULL, upl REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mode TEXT, view TEXT, notes TEXT,
  raw TEXT, tokens_in INTEGER, tokens_out INTEGER, cost REAL, latency REAL);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, ts REAL NOT NULL, type TEXT, inst TEXT,
  params TEXT, status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, inst TEXT, side TEXT, event TEXT,
  price REAL, contracts REAL, fee REAL, pnl REAL, trade_pnl REAL, reason TEXT, est INTEGER);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
"""


class JournalLocked(RuntimeError):
    """Another AEON process already owns this journal."""


def take_lock(path: str):
    """Exclusive, non-blocking lock on `<journal>.lock`, held for the life of the process (the OS drops it if the
    process dies). Two agents on one journal would both trade the same account: this makes the second one refuse to
    start, whether it is a stray `docker compose run`, a second container on the volume, or a forgotten terminal."""
    fh = open(path + ".lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise JournalLocked(f"another AEON process is already using the journal {path}. Two agents on one journal "
                            "would double-trade: stop the other one first (docker compose stop).") from None
    return fh


class Journal:
    def __init__(self, path: str = "aeon.db"):
        self._lock = threading.Lock()
        self._lock_fh = None if path == ":memory:" or path.startswith("file:") else take_lock(path)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(trades)")}
        if "execution_id" not in columns:
            self.db.execute("ALTER TABLE trades ADD COLUMN execution_id TEXT")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_execution ON trades(execution_id)")
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()
        if self._lock_fh is not None:
            self._lock_fh.close()                       # closing the file releases the lock
            self._lock_fh = None

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    # --- key/value (notes, risk state, paper broker state, token meters) -----
    def kv_get(self, key: str, default=None):
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def kv_set(self, key: str, value) -> None:
        self._exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    # --- equity curve --------------------------------------------------------
    def add_equity(self, equity: float, upl: float, ts: float | None = None) -> None:
        self._exec("INSERT INTO equity(ts,equity,upl) VALUES(?,?,?)", (ts or time.time(), equity, upl))

    def equity_series(self, since: float = 0.0, limit: int = 600) -> list[tuple[float, float]]:
        rows = self._query("SELECT ts,equity FROM equity WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, limit))
        return list(reversed(rows))

    # --- cycles / actions ----------------------------------------------------
    def add_cycle(self, mode: str, view: str, notes: str, raw: str, tin: int, tout: int,
                  cost: float, latency: float) -> int:
        cur = self._exec(
            "INSERT INTO cycles(ts,mode,view,notes,raw,tokens_in,tokens_out,cost,latency) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), mode, view, notes, raw, tin, tout, cost, latency))
        return int(cur.lastrowid)

    def last_cycle_ts(self) -> float | None:
        """When the last decision was booked, or None if there never was one."""
        return self._query("SELECT MAX(ts) FROM cycles")[0][0]

    def llm_usage(self, since: float = 0.0) -> dict:
        """Model calls, tokens and cost recorded since `since` (unix seconds; 0 = all time)."""
        r = self._query("SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), COALESCE(SUM(cost),0) "
                        "FROM cycles WHERE ts>=?", (since,))[0]
        return dict(calls=int(r[0]), tin=int(r[1]), tout=int(r[2]), cost=float(r[3]))

    def add_action(self, cycle_id: int | None, type_: str, inst: str, params: dict, status: str, detail: str) -> None:
        self._exec("INSERT INTO actions(cycle_id,ts,type,inst,params,status,detail) VALUES(?,?,?,?,?,?,?)",
                   (cycle_id, time.time(), type_, inst, json.dumps(params), status, detail))

    def recent_actions(self, n: int = 10) -> list[dict]:
        rows = self._query("SELECT ts,type,inst,status,detail FROM actions WHERE type!='hold' ORDER BY id DESC LIMIT ?", (n,))
        return [dict(ts=r[0], type=r[1], inst=r[2], status=r[3], detail=r[4]) for r in reversed(rows)]

    # --- trades --------------------------------------------------------------
    def _insert_trade(self, fill) -> bool:
        execution_id = getattr(fill, "execution_id", "") or None
        cursor = self.db.execute(
            "INSERT INTO trades(ts,inst,side,event,price,contracts,fee,pnl,trade_pnl,reason,est,execution_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(execution_id) DO NOTHING",
            (fill.ts, fill.inst, fill.side, fill.event, fill.price, fill.contracts, fill.fee,
             fill.pnl, fill.trade_pnl, fill.reason, int(fill.est), execution_id))
        return cursor.rowcount > 0

    def add_trade(self, fill) -> bool:
        with self._lock, self.db:
            return self._insert_trade(fill)

    def save_broker_state(self, key: str, state: dict, fills=()) -> None:
        from .broker import Fill

        pending_fills = [Fill(**value) for value in state.get("outbox", {}).values()]
        encoded = json.dumps(state, allow_nan=False)
        with self._lock, self.db:
            for fill in [*pending_fills, *fills]:
                self._insert_trade(fill)
            self.db.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded))

    def risk_activity(self, now: float | None = None) -> tuple[int, dict[str, float]]:
        current = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
        start = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        count = self._query("SELECT COUNT(*) FROM trades WHERE event IN ('open','add') AND ts>=?",
                            (start,))[0][0]
        closed = self._query("SELECT inst,MAX(ts) FROM trades WHERE event='close' GROUP BY inst")
        return count, dict(closed)

    def recent_trades(self, n: int = 8) -> list[dict]:
        rows = self._query(
            "SELECT ts,inst,side,event,price,pnl,trade_pnl,reason FROM trades ORDER BY id DESC LIMIT ?", (n,))
        return [dict(ts=r[0], inst=r[1], side=r[2], event=r[3], price=r[4], pnl=r[5], trade_pnl=r[6], reason=r[7])
                for r in reversed(rows)]

    def stats(self) -> dict:
        closed = self._query("SELECT trade_pnl FROM trades WHERE event='close' AND trade_pnl IS NOT NULL")
        pnls = [r[0] for r in closed]
        wins = sum(1 for p in pnls if p > 0)
        realized = self._query("SELECT COALESCE(SUM(pnl),0), COALESCE(SUM(fee),0) FROM trades")[0]
        return dict(closed=len(pnls), wins=wins, losses=len(pnls) - wins,
                    win_rate=(wins / len(pnls) * 100.0) if pnls else None,
                    realized=realized[0], fees=realized[1],
                    best=max(pnls) if pnls else None, worst=min(pnls) if pnls else None)
