import math

from ptbot.strategy import (OpenPlan, Params, Skip, cost_cap_hit, exit_distance, liq_distance, plan_open,
                            plan_rebalance, should_exit)


def test_100_usd_at_100x_meets_the_10k_minimum():
    plan = plan_open(100, 100, 10, 10_000, Params())
    assert isinstance(plan, OpenPlan)
    assert (plan.margin, plan.leverage, plan.notional) == (100, 100, 10_000)
    assert plan.close_move == 0.007


def test_exit_always_before_liquidation():
    p = Params(close_move=0.02)
    for lev in (50, 100, 200, 500):
        assert exit_distance(lev, p) <= liq_distance(lev) - p.liq_safety + 1e-12
    assert math.isclose(liq_distance(100), 0.0095)


def test_below_min_notional_skips_unless_leverage_allowed_to_rise():
    assert isinstance(plan_open(91.8, 91.8, 10, 10_000, Params()), Skip)
    plan = plan_open(91.8, 91.8, 10, 10_000, Params(max_leverage=150))
    assert isinstance(plan, OpenPlan)
    assert plan.leverage == 109 and plan.notional >= 10_000
    assert plan.close_move < liq_distance(109)


def test_margin_uses_the_smaller_wallet_and_cap():
    plan = plan_open(180, 120, 10, 10_000, Params(max_margin_usd=110))
    assert plan.margin == 110
    plan = plan_open(180, 120, 10, 10_000, Params())
    assert plan.margin == 120


def test_rebalance_moves_half_the_gap():
    t = plan_rebalance(30, 160, Params())
    assert (t.src, t.dst, t.amount) == ("B", "A", 65.0)
    assert plan_rebalance(100, 95, Params()) is None          # 2.6% gap: below threshold
    assert plan_rebalance(10, 16, Params(rebalance_threshold=0.05)) is None  # 3$ < min_rebalance_usd


def test_exit_trigger_both_directions():
    assert should_exit(100_000, 100_700, 0.007)
    assert should_exit(100_000, 99_300, 0.007)
    assert not should_exit(100_000, 100_600, 0.007)


def test_cost_cap_needs_a_full_window():
    p = Params(max_cost_per_1000=7, cost_window=3)
    assert cost_cap_hit([{"usd": 20, "paper": 1000}], p)[0] is False
    hist = [{"usd": 8, "paper": 1000}] * 3
    hit, avg = cost_cap_hit(hist, p)
    assert hit and math.isclose(avg, 8)
    assert not cost_cap_hit([{"usd": 5, "paper": 1000}] * 3, p)[0]


def test_small_gap_is_rebalanced_when_it_alone_blocks_the_min_notional():
    assert plan_rebalance(98.88, 103.0, Params()) is None
    t = plan_rebalance(98.88, 103.0, Params(), min_notional=10_000)
    assert (t.src, t.dst, t.amount) == ("B", "A", 2.06)


def test_default_cost_cap_judges_each_cycle():
    hit, avg = cost_cap_hit([{"usd": 9, "paper": 1000}], Params())
    assert hit and avg == 9
