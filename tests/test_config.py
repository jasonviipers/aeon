"""config.toml is edited by hand on the server: a bad number must stop the start, not quietly disable a limit."""
import pytest

from aeon.config import Config


def load(tmp_path, body: str) -> Config:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return Config.load(p)


def test_the_shipped_config_is_valid():
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
