import pytest

from aeon.brain import extract_json, parse_decision
from aeon.broker import Account, Position
from aeon.config import RiskCfg
from aeon.market import Spec
from aeon.risk import Proposal, RiskEngine


SPEC = Spec("SOL-USDT-SWAP", "SOL", ct_val=1.0, lot_sz=0.01, min_sz=0.01,
            tick_sz=0.01, max_lever=50)


def proposal(**values):
    fields = dict(type="open", inst=SPEC.inst, side="long", margin_pct=35, leverage=5,
                  stop_loss=0.9849, take_profit=1.0549)
    fields.update(values)
    return Proposal(**fields)


def account(positions=None):
    return Account(equity=200, available=200, balance=200, positions=positions or {})


def position(**values):
    fields = dict(inst=SPEC.inst, side="long", contracts=10, ct_val=1, entry=1,
                  lever=2, mark=1, sl=0.98, tp=1.05)
    fields.update(values)
    return Position(**fields)


@pytest.mark.parametrize("side,stop,target", [("long", 0.9849, 1.0549), ("short", 1.0151, 0.9451)])
def test_open_sizes_using_exchange_rounded_stop(side, stop, target):
    engine = RiskEngine(RiskCfg())
    verdict = engine.evaluate_open(proposal(side=side, stop_loss=stop, take_profit=target),
                                   account(), SPEC, 1, {SPEC.inst})
    assert verdict.ok
    assert verdict.stop_loss == (0.98 if side == "long" else 1.02)
    assert verdict.take_profit == (1.05 if side == "long" else 0.95)
    actual_loss = verdict.notional * (abs(1 - verdict.stop_loss) + 2 * engine.cfg.fee_rate)
    assert verdict.max_loss == pytest.approx(actual_loss)
    assert actual_loss <= 4 + 1e-9


@pytest.mark.parametrize("side,stop", [("long", 0.9951), ("short", 1.0049)])
def test_stop_rounding_to_price_is_rejected_for_open_and_adjust(side, stop):
    engine = RiskEngine(RiskCfg())
    prop = proposal(side=side, stop_loss=stop, take_profit=None)
    held = position(side=side, sl=0.98 if side == "long" else 1.02, tp=None)
    assert not engine.evaluate_open(prop, account(), SPEC, 1, {SPEC.inst}).ok
    assert not engine.evaluate_adjust(prop, held, 1, spec=SPEC).ok


@pytest.mark.parametrize("field,value", [("stop_loss", 0), ("take_profit", 0), ("take_profit", 1.0049)])
def test_invalid_rounded_protection_is_rejected(field, value):
    engine = RiskEngine(RiskCfg())
    prop = proposal(**{field: value})
    assert not engine.evaluate_open(prop, account(), SPEC, 1, {SPEC.inst}).ok
    assert not engine.evaluate_adjust(prop, position(), 1, spec=SPEC).ok


def test_rounded_stop_distance_is_checked():
    engine = RiskEngine(RiskCfg(min_stop_pct=1.2))
    prop = proposal(stop_loss=0.987)
    assert not engine.evaluate_open(prop, account(), SPEC, 1, {SPEC.inst}).ok
    assert not engine.evaluate_adjust(prop, position(), 1, spec=SPEC).ok
    engine = RiskEngine(RiskCfg(max_stop_pct=1.8))
    prop = proposal(stop_loss=0.9849)
    assert not engine.evaluate_open(prop, account(), SPEC, 1, {SPEC.inst}).ok
    assert not engine.evaluate_adjust(prop, position(), 1, spec=SPEC).ok


def test_adjust_returns_rounded_protection_and_matching_loss():
    engine = RiskEngine(RiskCfg())
    verdict = engine.evaluate_adjust(proposal(), position(sl=0.97), 1, spec=SPEC)
    assert verdict.ok
    assert (verdict.stop_loss, verdict.take_profit) == (0.98, 1.05)
    assert verdict.max_loss == pytest.approx(10 * (0.02 + 2 * engine.cfg.fee_rate))
    assert engine.evaluate_adjust(proposal(), position(sl=0.97), 1).stop_loss == 0.9849


def test_adjust_checks_tightening_after_rounding():
    verdict = RiskEngine(RiskCfg()).evaluate_adjust(proposal(), position(sl=0.984), 1, spec=SPEC)
    assert not verdict.ok
    assert "loosen" in verdict.why


def test_target_only_adjust_validates_inherited_rounded_stop():
    verdict = RiskEngine(RiskCfg()).evaluate_adjust(
        Proposal("adjust", SPEC.inst, take_profit=1.05), position(sl=0.9951), 1, spec=SPEC)
    assert not verdict.ok


def test_add_sizes_using_rounded_stop_and_combined_loss():
    held = position()
    engine = RiskEngine(RiskCfg())
    verdict = engine.evaluate_add(proposal(type="add"), account({SPEC.inst: held}), SPEC, 1)
    assert verdict.ok
    assert verdict.stop_loss == 0.98
    actual_loss = (verdict.notional + held.notional) * (0.02 + 2 * engine.cfg.fee_rate)
    assert verdict.max_loss == pytest.approx(actual_loss)
    assert actual_loss <= 4 + 1e-9


@pytest.mark.parametrize("pct", [0, -1, 101, float("nan"), float("inf")])
def test_reduce_rejects_invalid_explicit_percentages(pct):
    verdict = RiskEngine(RiskCfg()).evaluate_reduce(Proposal("reduce", SPEC.inst, pct=pct), position(), SPEC)
    assert not verdict.ok


def test_only_missing_reduce_percentage_defaults_to_half():
    verdict = RiskEngine(RiskCfg()).evaluate_reduce(Proposal("reduce", SPEC.inst), position(), SPEC)
    assert verdict.ok and verdict.contracts == 5


@pytest.mark.parametrize("pct", ["0", "0%", "-1%", "101%"])
def test_parsed_invalid_percentage_cannot_reduce_half_a_position(pct):
    decision = parse_decision({"actions": [dict(type="reduce", inst=SPEC.inst, pct=pct)]})
    verdict = RiskEngine(RiskCfg()).evaluate_reduce(decision.actions[0], position(), SPEC)
    assert not verdict.ok


@pytest.mark.parametrize("field", ["margin_pct", "leverage", "stop_loss", "sl", "take_profit", "tp", "pct", "confidence"])
@pytest.mark.parametrize("value", ["garbage", "1oops2", "2x3", "1,2", "", True, [], {}, "NaN", "1e400", 10**1000])
def test_malformed_supplied_action_numerics_reject_whole_action(field, value):
    decision = parse_decision({"actions": [dict(type="reduce", inst=SPEC.inst, **{field: value})]})
    assert not decision.actions
    assert decision.warnings and field in decision.warnings[0]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_constant_does_not_become_missing_percentage(constant):
    decision = parse_decision(extract_json('{"actions": [{"type": "reduce", "pct": ' + constant + '}]}'))
    assert not decision.actions
    assert decision.warnings


def test_numeric_formatting_and_absent_optional_fields_remain_supported():
    decision = parse_decision({"actions": [dict(type="open", inst=SPEC.inst, margin_pct="15%",
                                               leverage="3x", sl="$0.98", tp="1.05e0")]})
    assert not decision.warnings
    prop = decision.actions[0]
    assert (prop.margin_pct, prop.leverage, prop.stop_loss, prop.take_profit) == (15, 3, 0.98, 1.05)
    assert prop.pct is None and prop.confidence is None


def test_rejected_numeric_action_does_not_discard_valid_sibling():
    decision = parse_decision({"actions": [dict(type="reduce", pct="junk"), dict(type="hold")]})
    assert [action.type for action in decision.actions] == ["hold"]
    assert len(decision.warnings) == 1
