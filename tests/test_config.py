"""config.toml is edited by hand on the server: a bad number must stop the start, not quietly disable a limit."""
import pytest

from aeon.config import Config


def load(tmp_path, body: str) -> Config:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return Config.load(p)


def test_the_shipped_config_is_valid():
    Config.load("config.toml")


def test_the_shipped_config_is_safe_to_deploy_untouched(monkeypatch):
    """The file in the image is what runs if nobody edits anything. Pin the properties that make that acceptable,
    not the exact numbers, so a deliberate retune still passes but a slip (live mode, an uncapped budget, a halt that
    can never fire before the daily brake) does not."""
    monkeypatch.delenv("AEON_MODE", raising=False)
    cfg = Config.load("config.toml")
    r = cfg.risk
    assert cfg.run.mode in ("paper", "demo")                       # real money is a deliberate, separate switch
    assert cfg.run.brain == "llm"
    assert cfg.run.budget_usdt > 0                                 # 0 = "the whole account"
    assert set(cfg.universe.markets) <= {"swap", "spot"}           # the kinds `--smoke-demo` exercises on the exchange
    assert r.stops_only_tighten and r.one_position_per_asset
    assert r.max_leverage <= 3 and r.risk_per_trade_pct <= 1.0
    assert r.daily_loss_limit_pct >= r.risk_per_trade_pct * r.max_open_positions     # one bad moment is not a "day"
    assert r.max_drawdown_pct > r.daily_loss_limit_pct              # the day brake comes first, the halt after it
    assert r.max_drawdown_pct <= 15
    assert 2 * r.fee_rate * 100 <= 0.15 * r.min_stop_pct            # a round trip's fees stay a small part of what the tightest stop risks
    assert cfg.notify.enabled and "risk" in cfg.notify.events and "error" in cfg.notify.events and cfg.notify.ping_on_halt


def test_the_mode_can_be_chosen_from_the_environment_without_editing_the_file(monkeypatch):
    monkeypatch.setenv("AEON_MODE", "paper")
    assert Config.load("config.toml").run.mode == "paper"
    monkeypatch.setenv("AEON_MODE", "nonsense")
    with pytest.raises(ValueError, match="run.mode"):
        Config.load("config.toml")


@pytest.mark.parametrize("body, message", [
    ("[risk]\nrisk_per_trade_pct = nan", "finite"),
    ("[risk]\nmax_drawdown_pct = inf", "finite"),
    ("[risk]\nrisk_per_trade_pct = 0", "risk_per_trade_pct"),
    ("[risk]\nrisk_per_trade_pct = 150", "risk_per_trade_pct"),
    ("[risk]\nmin_stop_pct = 5.0\nmax_stop_pct = 2.0", "min_stop_pct"),
    ("[risk]\nmax_open_positions = 0", "max_open_positions"),
    ("[risk]\nmax_trades_per_day = 0", "max_trades_per_day"),
    ("[risk]\nfutures_close_days_before = 5.0\nfutures_min_days_to_open = 3.0", "opened and closed again"),
    ("[risk]\nfee_rate = 5", "fee_rate"),
    ("[run]\nmonitor_seconds = 0", "monitor_seconds"),
    ("[run]\nmonitor_seconds = 600", "monitor_seconds"),
    ("[run]\nbudget_usdt = -100", "budget_usdt"),
    ("[run]\nstart_equity = 0", "start_equity"),
    ("[run]\nmin_cycle_min = 0", "min_cycle_min"),
    ("[run]\nslippage = 2.0", "slippage"),
    ("[run]\ndefault_cycle_min = 7.5", "whole number"),
    ("[universe]\nmarkets = \"swap\"", "list of text"),
    ("[universe]\nmarkets = [1, 2]", "list of text"),
    ("[universe]\nmode = \"list\"\ninstruments = []", "instruments"),
    ("[llm]\nbase_url = \"http://api.example.com/v1\"", "https://"),
    ("[llm]\ntemperature = 9", "temperature"),
    ("[llm]\ntimeout_s = 0", "timeout_s"),
    ("[notify]\nmax_per_minute = 0", "max_per_minute"),
])
def test_bad_values_are_rejected_with_a_message_that_names_the_setting(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        load(tmp_path, body)


def test_a_local_model_server_may_use_plain_http(tmp_path):
    assert load(tmp_path, '[llm]\nbase_url = "http://localhost:11434/v1"').llm.base_url.startswith("http://localhost")


def test_budget_zero_means_the_whole_account(tmp_path):
    assert load(tmp_path, "[run]\nbudget_usdt = 0").run.budget_usdt == 0
