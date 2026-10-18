"""Configuration: config.toml for behaviour, .env / environment for secrets."""
from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

LIVE_CONFIRM_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class RunCfg:
    mode: str = "paper"
    brain: str = "llm"
    start_equity: float = 200.0
    default_cycle_min: int = 15
    min_cycle_min: int = 5
    max_cycle_min: int = 60
    monitor_seconds: float = 5.0
    db_path: str = ""                # "" = automatic: aeon-paper.db / aeon-demo.db / aeon-live.db (modes never share a journal)
    slippage: float = 0.0002
    budget_usdt: float = 200.0       # demo/live: the most AEON may treat as its capital (0 = the whole account)


@dataclass
class UniverseCfg:
    mode: str = "top_volume"
    markets: list[str] = field(default_factory=lambda: ["swap", "futures", "spot"])
    top_n: int = 12                  # perpetuals offered to the agent each cycle
    top_n_futures: int = 5           # dated futures (one expiry per coin)
    top_n_spot: int = 6
    min_volume_usd: float = 30_000_000
    futures_min_volume_usd: float = 100_000     # dated futures are thin; they would never pass the bar above
    futures_include_xperp: bool = False         # OKX's 5-year "XPERP" contracts behave like perpetuals
    only_affordable: bool = True
    categories: list[str] = field(default_factory=lambda: ["crypto"])
    instruments: list[str] = field(
        default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    )


@dataclass
class RiskCfg:
    max_leverage: float = 5.0
    risk_per_trade_pct: float = 2.0
    max_margin_pct: float = 35.0
    max_open_positions: int = 3
    max_exposure_x: float = 3.0
    min_stop_pct: float = 0.4
    max_stop_pct: float = 8.0
    stops_only_tighten: bool = True
    daily_loss_limit_pct: float = 8.0
    max_drawdown_pct: float = 25.0
    reentry_cooldown_min: int = 30
    max_trades_per_day: int = 24
    fee_rate: float = 0.0005          # perpetual / futures taker fee assumption
    spot_fee_rate: float = 0.001      # spot taker fee assumption
    one_position_per_asset: bool = True   # no BTC spot + BTC perp + BTC future at once
    futures_min_days_to_open: float = 3.0     # never open a dated future this close to expiry
    futures_close_days_before: float = 1.0    # close any dated future this many days before it expires


NOTIFY_EVENTS = ("fill", "action", "risk", "error", "system")   # "think" is deliberately not offered


@dataclass
class NotifyCfg:
    enabled: bool = False
    webhook_env: str = "DISCORD_WEBHOOK_URL"      # which .env variable holds the Discord webhook URL
    events: list[str] = field(default_factory=lambda: ["fill", "risk", "error"])
    startup: bool = True                          # post one "AEON online" message on every boot
    cooldown_s: float = 60.0                      # identical messages inside this window are dropped
    max_per_minute: int = 20                      # burst cap; anything above is dropped
    ping_on_halt: bool = True                     # @here when the kill switch fires / AEON halts


@dataclass
class LlmCfg:
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "anthropic/claude-sonnet-4.5"
    api_key_env: str = "LLM_API_KEY"
    temperature: float = 0.3
    max_tokens: int = 2000
    timeout_s: float = 90.0
    json_mode: bool = False
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0
    referer: str = "https://localhost/aeon"
    extra_body: dict = field(default_factory=dict)   # provider-specific request fields, e.g. DeepSeek's thinking switch


def _merge(obj: object, values: dict, section: str) -> None:
    for key, value in values.items():
        if not hasattr(obj, key):
            raise ValueError(f"config.toml: unknown key [{section}] {key!r}")
        current = getattr(obj, key)
        if isinstance(current, bool):
            if not isinstance(value, bool):
                raise ValueError(f"config.toml: [{section}] {key} must be true/false")
        elif isinstance(current, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"config.toml: [{section}] {key} must be a number")
            if isinstance(current, int) and float(value) != int(value):
                raise ValueError(f"config.toml: [{section}] {key} must be a whole number (got {value})")
            value = type(current)(value)
        elif isinstance(current, str) and not isinstance(value, str):
            raise ValueError(f"config.toml: [{section}] {key} must be text")
        elif isinstance(current, list) and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise ValueError(f"config.toml: [{section}] {key} must be a list of text values, e.g. [\"swap\", \"spot\"]")
        elif isinstance(current, dict) and not isinstance(value, dict):
            raise ValueError(f"config.toml: [{section}] {key} must be a table, e.g. {{ thinking = {{ type = \"disabled\" }} }}")
        setattr(obj, key, value)


@dataclass
class Config:
    run: RunCfg = field(default_factory=RunCfg)
    universe: UniverseCfg = field(default_factory=UniverseCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> "Config":
        cfg = cls()
        p = Path(path)
        raw = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        sections = ("run", "universe", "risk", "llm", "notify")
        for section in raw:
            if section not in sections:
                raise ValueError(f"config.toml: unknown section [{section}]")
        for name in sections:
            _merge(getattr(cfg, name), raw.get(name, {}), name)
        env_mode = os.environ.get("AEON_MODE")
        if env_mode:
            cfg.run.mode = env_mode
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.run.mode not in ("paper", "demo", "live"):
            raise ValueError("run.mode must be paper, demo or live")
        if self.run.brain not in ("llm", "heuristic"):
            raise ValueError("run.brain must be llm or heuristic")
        if self.universe.mode not in ("top_volume", "list"):
            raise ValueError("universe.mode must be top_volume or list")
        bad_markets = [m for m in self.universe.markets if m not in ("swap", "futures", "spot")]
        if bad_markets or not self.universe.markets:
            raise ValueError(f"universe.markets: use any of swap, futures, spot (got {self.universe.markets})")
        bad = [c for c in self.universe.categories if c not in ("crypto", "stocks", "commodities")]
        if bad:
            raise ValueError(f"universe.categories: unknown {bad}; use crypto, stocks, commodities")
        if not 1 <= self.risk.max_leverage <= 125:
            raise ValueError("risk.max_leverage must be between 1 and 125")
        if self.run.min_cycle_min > self.run.max_cycle_min:
            raise ValueError("run.min_cycle_min must be <= run.max_cycle_min")
        bad_events = [e for e in self.notify.events if e not in NOTIFY_EVENTS]
        if bad_events or not self.notify.events:
            raise ValueError(f"notify.events: use any of {', '.join(NOTIFY_EVENTS)} (got {self.notify.events})")
        if self.notify.cooldown_s < 0:
            raise ValueError("notify.cooldown_s must be 0 or more")
        if self.notify.max_per_minute < 1:
            raise ValueError("notify.max_per_minute must be 1 or more")
        self._validate_numbers()

    def _validate_numbers(self) -> None:
        """Every number must be finite and sane. TOML accepts `nan` and `inf`, and a zero or negative limit would
        not fail loudly: it would silently disable a safety check or spin a loop."""
        for section in ("run", "universe", "risk", "llm", "notify"):
            for f in fields(getattr(self, section)):
                v = getattr(getattr(self, section), f.name)
                if isinstance(v, float) and not math.isfinite(v):
                    raise ValueError(f"{section}.{f.name} must be a finite number (got {v})")

        def need(ok: bool, msg: str) -> None:
            if not ok:
                raise ValueError(msg)

        r, run, u, llm = self.risk, self.run, self.universe, self.llm
        need(run.start_equity > 0, "run.start_equity must be above 0")
        need(run.budget_usdt >= 0, "run.budget_usdt must be 0 (use the whole account) or a positive amount")
        need(1 <= run.min_cycle_min <= run.max_cycle_min, "run: need 1 <= min_cycle_min <= max_cycle_min")
        need(run.default_cycle_min >= 1, "run.default_cycle_min must be 1 or more")
        need(1.0 <= run.monitor_seconds <= 60.0, "run.monitor_seconds must be between 1 and 60 "
                                                  "(0 would hammer the exchange; the monitor is what re-arms stops)")
        need(0.0 <= run.slippage <= 0.05, "run.slippage must be between 0 and 0.05 (a fraction: 0.0002 = 0.02%)")
        need(u.top_n >= 0 and u.top_n_futures >= 0 and u.top_n_spot >= 0, "universe.top_n* must be 0 or more")
        need(u.min_volume_usd >= 0 and u.futures_min_volume_usd >= 0, "universe volume floors must be 0 or more")
        need(u.mode != "list" or u.instruments, "universe.mode = \"list\" needs at least one entry in universe.instruments")
        need(1 <= r.max_leverage <= 125, "risk.max_leverage must be between 1 and 125")
        for name in ("risk_per_trade_pct", "max_margin_pct", "daily_loss_limit_pct", "max_drawdown_pct"):
            need(0 < getattr(r, name) <= 100, f"risk.{name} must be above 0 and at most 100 (a percentage)")
        need(r.max_open_positions >= 1, "risk.max_open_positions must be 1 or more")
        need(r.max_exposure_x > 0, "risk.max_exposure_x must be above 0")
        need(0 < r.min_stop_pct < r.max_stop_pct <= 100, "risk: need 0 < min_stop_pct < max_stop_pct <= 100")
        need(r.reentry_cooldown_min >= 0, "risk.reentry_cooldown_min must be 0 or more")
        need(r.max_trades_per_day >= 1, "risk.max_trades_per_day must be 1 or more")
        need(0 <= r.fee_rate < 0.05 and 0 <= r.spot_fee_rate < 0.05,
             "risk.fee_rate / spot_fee_rate are fractions between 0 and 0.05 (0.0005 = 0.05%)")
        need(r.futures_min_days_to_open >= 0 and r.futures_close_days_before >= 0,
             "risk.futures_*_days must be 0 or more")
        need(r.futures_close_days_before < r.futures_min_days_to_open,
             "risk: futures_close_days_before must be below futures_min_days_to_open, or a future would be "
             "opened and closed again at once")
        need(0 <= llm.temperature <= 2, "llm.temperature must be between 0 and 2")
        need(llm.max_tokens >= 1 and llm.timeout_s > 0, "llm.max_tokens must be 1 or more and llm.timeout_s above 0")
        need(llm.input_price_per_m >= 0 and llm.output_price_per_m >= 0, "llm prices must be 0 or more")
        local = llm.base_url.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))
        need(llm.base_url.startswith("https://") or local,
             "llm.base_url must be https:// (your API key is sent to it); plain http is only allowed for localhost")

    # --- secrets -------------------------------------------------------------
    def okx_credentials(self) -> tuple[str, str, str]:
        return (
            os.environ.get("OKX_API_KEY", ""),
            os.environ.get("OKX_API_SECRET", ""),
            os.environ.get("OKX_PASSPHRASE", ""),
        )

    def llm_key(self) -> str:
        return os.environ.get(self.llm.api_key_env, "")

    def live_armed(self) -> bool:
        return os.environ.get("AEON_CONFIRM_LIVE", "") == LIVE_CONFIRM_PHRASE
