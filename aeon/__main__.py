"""Entry point:  python -m aeon [--headless | --once | --check] [--mode paper|demo|live] [--brain llm|heuristic]"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

REQUIRED = ("httpx", "textual", "websockets")


def _missing_dependency() -> str | None:
    for name in REQUIRED:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            return name
    return None


def _venv_python(root: Path) -> Path | None:
    for rel in ("Scripts/python.exe", "bin/python"):
        p = root / ".venv" / rel
        if p.exists():
            return p
    return None


def _relaunch_target(root: Path, current: str, already: bool) -> Path | None:
    """The project's own virtual-environment Python, if this is a different interpreter and we haven't switched yet."""
    venv = _venv_python(root)
    if venv is None or already:
        return None
    return venv if Path(current).resolve() != venv.resolve() else None


def _ensure_dependencies() -> None:
    """`python -m aeon` with a Python that lacks AEON's packages (the usual case: your system Python, when the
    packages live in the project's .venv) re-runs itself with the .venv. With no .venv it explains the fix
    instead of dying on an ImportError."""
    missing = _missing_dependency()
    if missing is None:
        return
    root = Path(__file__).resolve().parent.parent
    target = _relaunch_target(root, sys.executable, os.environ.get("AEON_RELAUNCHED") == "1")
    if target is not None:
        print(f"[aeon] this Python has no '{missing}'; using the project's virtual environment instead ({target})",
              file=sys.stderr, flush=True)
        raise SystemExit(subprocess.call([str(target), "-m", "aeon", *sys.argv[1:]], cwd=os.getcwd(),
                                         env=dict(os.environ, AEON_RELAUNCHED="1")))
    raise SystemExit(
        f"AEON can't start: the Python package '{missing}' is not installed for this Python ({sys.executable}).\n"
        "Fix it with one of:\n"
        "  aeon.bat                        creates a private virtual environment and installs everything, or\n"
        "  python -m pip install -e .      run this inside the project folder")


_ensure_dependencies()

import argparse  # noqa: E402  (these need the packages checked above)
import asyncio  # noqa: E402
import time  # noqa: E402

from .agent import Agent, Event  # noqa: E402
from .brain import HeuristicBrain, LlmBrain
from .broker import PaperBroker
from .config import LIVE_CONFIRM_PHRASE, Config, load_dotenv
from .feed import PriceFeed
from .journal import Journal
from .market import Market
from .notify import DiscordNotifier
from .okx import OkxClient, OkxError
from .okx_broker import OkxBroker
from .risk import RiskEngine


class Setup:
    """Everything wired together, plus the clients that need closing."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.journal = Journal(cfg.run.db_path or f"aeon-{cfg.run.mode}.db")      # modes never share a journal
        self.public = OkxClient()                       # market data never needs credentials
        self.market = Market(self.public, cfg.risk, cfg.universe)
        self.feed = PriceFeed(self.public)
        self.risk = RiskEngine(cfg.risk, self.journal)
        self.trade_client: OkxClient | None = None
        self.llm_client = None
        mode = cfg.run.mode
        if mode == "paper":
            self.broker = PaperBroker(cfg.run.start_equity, cfg.risk.fee_rate, cfg.run.slippage, cfg.risk.spot_fee_rate)
        else:
            key, secret, phrase = cfg.okx_credentials()
            if not (key and secret and phrase):
                raise SystemExit(f"mode={mode} needs OKX_API_KEY, OKX_API_SECRET and OKX_PASSPHRASE in .env"
                                 + (" (create them under OKX 'Demo Trading')" if mode == "demo" else ""))
            if mode == "live" and not cfg.live_armed():
                raise SystemExit("mode=live trades REAL MONEY and is not armed.\n"
                                 f"Once you have run paper and demo and are sure, set AEON_CONFIRM_LIVE={LIVE_CONFIRM_PHRASE} in .env")
            self.trade_client = OkxClient(key, secret, phrase, demo=(mode == "demo"))
            self.broker = OkxBroker(self.trade_client, self.market.specs, label=mode, kinds=set(cfg.universe.markets))
        if cfg.run.brain == "heuristic":
            self.brain = HeuristicBrain()
        else:
            if not cfg.llm_key():
                raise SystemExit(f"brain=llm needs {cfg.llm.api_key_env} in .env "
                                 "(or run with --brain heuristic to try the system without an LLM)")
            self.brain = LlmBrain(cfg.llm, cfg.risk, cfg.llm_key())
            self.llm_client = self.brain.client
        self.notify = DiscordNotifier(cfg.notify, mode=mode, label=self.brain.label)
        self.agent = Agent(cfg, self.market, self.feed, self.broker, self.risk, self.brain, self.journal,
                           notify=self.notify)

    async def aclose(self) -> None:
        await self.agent.stop()
        for c in (self.public, self.trade_client, self.llm_client, self.notify):
            if c:
                await c.aclose()


def print_event(ev: Event) -> None:
    stamp = time.strftime("%H:%M:%S", time.gmtime(ev.ts))
    print(f"[{stamp}] {ev.kind.upper():<6} {ev.text}", flush=True)


async def run_headless(setup: Setup, once: bool) -> None:
    agent = setup.agent
    agent.on_event = print_event
    await agent.start(decisions=not once)
    try:
        if once:
            await asyncio.sleep(1)
            await agent.cycle()
            await asyncio.sleep(2)
        else:
            await asyncio.Event().wait()
    finally:
        await setup.aclose()


ACCT_LEVELS = {
    "1": "Spot mode (spot only; cannot trade perpetuals or futures)",
    "2": "Spot and futures mode (spot, margin, perpetuals, expiry futures, options)",
    "3": "Multi-currency margin (spot, margin, perpetuals, expiry futures, options)",
    "4": "Portfolio margin (spot, margin, perpetuals, expiry futures, options)",
}

# The five product groups in OKX's "Markets" filter: (label, OKX instrument type, AEON's name for it, or None if not built)
MARKETS = [("Spot", "SPOT", "spot"), ("Margin", "MARGIN", None), ("Perpetual", "SWAP", "swap"),
           ("Expiry futures", "FUTURES", "futures"), ("Options", "OPTION", None)]


async def market_coverage(pub: OkxClient, acct_lvl: str | None, op_auth: bool | None, enabled: list[str]) -> None:
    print("  Markets: what exists on OKX, what your account allows, what AEON trades")
    for label, kind, aeon_kind in MARKETS:
        supported = "not yet" if aeon_kind is None else "TRADES IT" if aeon_kind in enabled else "off in config"
        try:
            rows = await pub.instruments(kind, "BTC-USD" if kind == "OPTION" else None)
            count = f"{len(rows):>5} instruments" + (" (BTC-USD family)" if kind == "OPTION" else "")
        except OkxError as e:
            count = f"count unavailable ({e.msg[:30]})"
        if acct_lvl is None:
            allowed = "n/a (no account checked)"
        elif kind == "OPTION":
            allowed = "yes" if op_auth else "NO: options trading is not enabled on this account"
        elif kind in ("MARGIN", "SWAP", "FUTURES"):
            allowed = "yes" if acct_lvl in ("2", "3", "4") else "NO: needs a higher account level"
        else:
            allowed = "yes"
        print(f"      {label:<15} {count:<34} AEON: {supported:<13} account: {allowed}")
    print(f"      {'Events':<15} {'separate prediction-market API':<34} AEON: {'not yet':<13} account: n/a")


async def run_check(cfg: Config) -> int:
    ok = True
    acct_lvl: str | None = None
    op_auth: bool | None = None

    def line(good: bool, text: str) -> None:
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else '!!'}] {text}")

    print(f"AEON check | mode={cfg.run.mode} brain={cfg.run.brain}")
    pub = OkxClient()
    market = Market(pub, cfg.risk, cfg.universe)
    try:
        await market.load_specs()
        counts = {k: sum(1 for s in market.specs.values() if s.kind == k) for k in ("swap", "futures", "spot")}
        line(True, f"OKX public API reachable | tradable here: {counts['swap']} perps, {counts['futures']} dated futures, "
                   f"{counts['spot']} spot pairs (categories {cfg.universe.categories}, markets {cfg.universe.markets})")
    except OkxError as e:
        line(False, f"OKX public API: {e}")
    if cfg.run.mode != "paper":
        key, secret, phrase = cfg.okx_credentials()
        line(bool(key and secret and phrase), "OKX credentials present in .env")
        if key and secret and phrase:
            demo = cfg.run.mode == "demo"
            c = OkxClient(key, secret, phrase, demo=demo)
            try:
                rows = await c.account_config()
                row = rows[0] if rows else {}
                acct_lvl, op_auth = str(row.get("acctLv")), str(row.get("opAuth")) == "1"
                line(True, f"OKX private API reachable in the {'DEMO' if demo else 'LIVE'} environment")
                lost = sorted(set(cfg.universe.markets) & {"swap", "futures"})
                if acct_lvl == "1" and "spot" in cfg.universe.markets:
                    print(f"  [..] account level 1: {ACCT_LEVELS['1']}. AEON will trade spot only"
                          + (f"; {' and '.join(lost)} stay off until you switch the OKX account mode to 'Spot and futures mode'" if lost else ""))
                else:
                    line(acct_lvl in ACCT_LEVELS and acct_lvl != "1", f"account level {acct_lvl}: {ACCT_LEVELS.get(acct_lvl, 'unknown')}")
                perms = [p.strip() for p in str(row.get("perm", "")).split(",") if p.strip()]
                if "withdraw" in perms:
                    line(False, "this API key has WITHDRAW permission: delete it and create a Trade-only key")
                elif perms and "trade" not in perms:
                    line(False, f"API key permission is {perms}: it can read but not trade")
                else:
                    line(True, f"API key permission: {', '.join(perms) or 'not reported'} (no withdraw)")
                b = OkxBroker(c, market.specs, kinds=set(cfg.universe.markets))
                await b.init()
                acct = await b.account({})
                line(True, f"account equity {acct.equity:,.2f} USDT | position mode {b.pos_mode} | AEON may trade: {', '.join(sorted(b.allowed_kinds))}")
                if cfg.run.budget_usdt > 0:
                    print(f"  [..] capital cap: AEON treats only {cfg.run.budget_usdt:,.0f} USDT as its capital (run.budget_usdt), "
                          f"however much else is in the account")
            except Exception as e:
                line(False, f"OKX private API: {e}")
            await c.aclose()
        if cfg.run.mode == "live":
            line(cfg.live_armed(), "live trading armed (AEON_CONFIRM_LIVE)")
    await market_coverage(pub, acct_lvl, op_auth, cfg.universe.markets)
    if cfg.run.brain == "llm":
        key = cfg.llm_key()
        line(bool(key), f"{cfg.llm.api_key_env} present")
        if key:
            from .brain import LlmClient
            client = LlmClient(cfg.llm, key)
            try:
                text, tin, tout = await client.chat(
                    [{"role": "user", "content": 'Reply with only this JSON object: {"ok": true}'}])
                line(True, f"LLM reachable: {cfg.llm.model} replied {text.strip()[:20]!r} ({tin}+{tout} tokens)")
            except Exception as e:
                line(False, f"LLM: {e}")
            await client.aclose()
    if cfg.notify.enabled:
        notifier = DiscordNotifier(cfg.notify, mode=cfg.run.mode, label=cfg.run.brain)
        if notifier.problem:
            line(False, notifier.problem)
        else:
            try:
                await notifier.test()
                line(True, f"Discord alerts reachable: a test message was posted to {cfg.notify.webhook_env}")
            except Exception as e:
                line(False, f"Discord alerts: {e}")
        await notifier.aclose()
    else:
        print("  [..] Discord alerts are off ([notify] enabled = false); no webhook checked")
    await pub.aclose()
    print("ready" if ok else "fix the items marked !! before running")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(prog="aeon", description="Autonomous LLM trader for OKX perpetuals, dated futures and spot")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--mode", choices=["paper", "demo", "live"])
    ap.add_argument("--brain", choices=["llm", "heuristic"])
    ap.add_argument("--db", help="journal database file (default from config.toml); use a scratch file for experiments")
    ap.add_argument("--headless", action="store_true", help="no UI; log events to stdout (for a server)")
    ap.add_argument("--once", action="store_true", help="run exactly one decision cycle, print it, exit")
    ap.add_argument("--check", action="store_true", help="verify config, keys and connectivity, then exit")
    args = ap.parse_args()

    load_dotenv()
    try:
        cfg = Config.load(args.config)
    except ValueError as e:
        raise SystemExit(f"config error: {e}")
    if args.mode:
        cfg.run.mode = args.mode
    if args.brain:
        cfg.run.brain = args.brain
    if args.db:
        cfg.run.db_path = args.db
    try:
        cfg.validate()
    except ValueError as e:
        raise SystemExit(f"config error: {e}")

    if args.check:
        raise SystemExit(asyncio.run(run_check(cfg)))
    setup = Setup(cfg)
    if args.headless or args.once:
        try:
            asyncio.run(run_headless(setup, once=args.once))
        except KeyboardInterrupt:
            print("stopped; open positions keep their exchange-side stops", file=sys.stderr)
        return
    from .tui import AeonApp
    AeonApp(setup).run()


if __name__ == "__main__":
    main()
