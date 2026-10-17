import math

from ptbot.strategy import (OpenPlan, Params, Skip, cost_cap_hit, exit_distance, liq_distance, plan_open,
                            plan_rebalance, should_exit)

from . import x100


def test_100_usd_at_100x_meets_the_10k_minimum():
    plan = plan_open(100, 100, 10, 10_000, x100())
    assert isinstance(plan, OpenPlan)
    assert (plan.margin, plan.leverage, plan.notional) == (100, 100, 10_000)
    assert plan.close_move == 0.007


def test_exit_always_before_liquidation():
    p = x100(close_move=0.02)
    for lev in (50, 100, 200, 500):
        assert exit_distance(lev, p) <= liq_distance(lev) - p.liq_safety + 1e-12
    assert math.isclose(liq_distance(100), 0.0095)


def test_below_min_notional_skips_unless_leverage_allowed_to_rise():
    assert isinstance(plan_open(91.8, 91.8, 10, 10_000, x100()), Skip)
    plan = plan_open(91.8, 91.8, 10, 10_000, x100(max_leverage=150))
    assert isinstance(plan, OpenPlan)
    assert plan.leverage == 109 and plan.notional >= 10_000
    assert plan.close_move < liq_distance(109)


def test_margin_uses_the_smaller_wallet_and_cap():
    plan = plan_open(180, 120, 10, 10_000, x100(max_margin_usd=110))
    assert plan.margin == 110
    plan = plan_open(180, 120, 10, 10_000, x100())
    assert plan.margin == 120


def test_rebalance_moves_half_the_gap():
    t = plan_rebalance(30, 160, x100())
    assert (t.src, t.dst, t.amount) == ("B", "A", 65.0)
    assert plan_rebalance(100, 95, x100()) is None          # 2.6% gap: below threshold
    assert plan_rebalance(10, 16, x100(rebalance_threshold=0.05)) is None  # 3$ < min_rebalance_usd


def test_exit_trigger_both_directions():
    assert should_exit(100_000, 100_700, 0.007)
    assert should_exit(100_000, 99_300, 0.007)
    assert not should_exit(100_000, 100_600, 0.007)


def test_cost_cap_needs_a_full_window():
    p = x100(max_cost_per_1000=7, cost_window=3)
    assert cost_cap_hit([{"usd": 20, "paper": 1000}], p)[0] is False
    hist = [{"usd": 8, "paper": 1000}] * 3
    hit, avg = cost_cap_hit(hist, p)
    assert hit and math.isclose(avg, 8)
    assert not cost_cap_hit([{"usd": 5, "paper": 1000}] * 3, p)[0]


def test_small_gap_is_rebalanced_when_it_alone_blocks_the_min_notional():
    p = x100(rebalance_threshold=0.2)
    assert plan_rebalance(95, 125, p) is None                          # 13.6% gap: under the threshold
    t = plan_rebalance(95, 125, p, mins=(10, 10_000))                   # ...but it blocks a 10k pair
    assert (t.src, t.dst, t.amount) == ("B", "A", 15.0)
    assert plan_rebalance(98.88, 103.0, p, mins=(10, 10_000)) is None   # 2.06$: under the 11 USDC a transfer needs


def test_default_cost_cap_judges_each_cycle():
    hit, avg = cost_cap_hit([{"usd": 9, "paper": 1000}], x100())
    assert hit and avg == 9


def test_a_cycle_that_made_money_costs_nothing():
    from ptbot.strategy import cost_per_1000
    assert cost_per_1000(-5.0, 0) == 0.0
    assert cost_per_1000(5.0, 0) == math.inf


def test_blocking_rebalance_follows_the_margin_fraction():
    p = x100(margin_fraction=0.5)
    t = plan_rebalance(215, 185, p, mins=(10, 10_000))
    assert (t.src, t.dst, t.amount) == ("A", "B", 15.0)
    assert plan_rebalance(100.40, 99.70, x100(), mins=(10, 10_000)) is None   # 0.35$: not worth a transfer


def test_default_sizing_is_11k_notional_at_250x():
    p = Params()                                  # LEVERAGE=MAX_LEVERAGE=250, TARGET_NOTIONAL_USD=11000
    plan = plan_open(100, 100, 10, 10_000, p)
    assert (plan.margin, plan.leverage, plan.notional) == (44, 250, 11_000)
    assert math.isclose(plan.close_move, 1 / 250 - 0.0005 - 0.0015)   # 0.20%: well before liquidation (0.35%)
    plan = plan_open(42.5, 300, 10, 10_000, p)    # not enough for 11k: smaller, still above the minimum
    assert (plan.margin, plan.leverage) == (42.5, 250)
    assert isinstance(plan_open(39.9, 39.9, 10, 10_000, p), Skip)      # 9,975$ < 10,000$ at x250
    assert plan_open(100, 100, 10, 10_000, Params(leverage=200, max_leverage=250)).margin == 55


def test_rebalance_moves_whole_usdc():
    t = plan_rebalance(43.84, 66.45, x100())
    assert (t.src, t.dst, t.amount) == ("B", "A", 11.0)


def test_rebalance_never_under_the_core_transfer_minimum():
    from ptbot.strategy import MIN_CORE_TRANSFER_USD
    assert plan_rebalance(40, 60, x100(min_rebalance_usd=1)) is None    # 10$ < 11 USDC, whatever the setting
    assert plan_rebalance(38, 62, x100()).amount == 12.0 >= MIN_CORE_TRANSFER_USD


def test_win_payout_matches_real_trades():
    from ptbot.strategy import cost_per_1000_at, win_payout
    # position 596121: short, 1.6M$ notional, entry 83378.5 -> exit 82687.5; adjusted 11639.71$ before the 2% fee
    move = (83378.5 - 82687.5) / 83378.5
    assert abs(1_600_000 * win_payout(move) / 0.98 - 11639.71) < 2
    # position 573718: short, 40k$ notional, entry 83668.5 -> exit 82676.5; adjusted 419.43$
    move = (83668.5 - 82676.5) / 83668.5
    assert abs(40_000 * win_payout(move) / 0.98 - 419.43) < 0.1
    assert win_payout(0.00001) == 0.0
    assert abs(cost_per_1000_at(0.02, 18.9) - 6.87) < 0.01 and abs(cost_per_1000_at(0.002, 18.9) - 10.96) < 0.01


def test_ladder_sizing_lets_the_losing_side_pay_a_whole_period():
    from ptbot.strategy import cost_per_1000_at, ladder_reset_move, plan_ladder
    p = Params(strategy="ladder", ladder_step=0.0025, max_cost_per_1000=7.0)
    reset = ladder_reset_move(18.9, p)
    assert cost_per_1000_at(reset, 18.9) <= 7.0 * 0.98 < cost_per_1000_at(reset - 0.0005, 18.9)
    two = plan_ladder({"A": 54.1, "B": 55.4}, 10, 1000, 18.9, p)
    assert two.reset == reset and two.close_move == 0.0025
    assert 1 / two.leverage - 0.0005 - 0.0025 >= p.ladder_liq_buffer - 1e-9      # liquidation stays 0.25% away
    assert 2 * two.margin + (reset + 0.0025) * two.notional <= 54.1 + 0.01      # B alone can pay a whole period
    one = plan_ladder({"A": 109.0, "B": 0.0}, 10, 1000, 18.9, Params(strategy="ladder", one_wallet=True))
    assert one.close_move == 0.002 and one.leverage == 200                     # defaults: step 0.20%, liquidation 0.45%
    assert 3 * one.margin + (one.reset + one.close_move) * one.notional <= 109.0 + 0.01
    assert one.notional > 1.6 * two.notional                                    # one pool funds both sides
    small = plan_ladder({"A": 50.0, "B": 50.0}, 10, 1000, 18.9, p)               # min margin: lower leverage
    assert 10 < small.margin < 10.5 and small.leverage < two.leverage            # room to shrink with the price
    assert isinstance(plan_ladder({"A": 12.0, "B": 12.0}, 10, 1000, 18.9, p), Skip)
    assert isinstance(plan_ladder({"A": 54.0, "B": 54.0}, 10, 1000, 16.5, p), Skip)  # no move is cheap enough
    assert plan_ladder({"A": 60.0, "B": 60.0}, 10, 1000, None, Params(ladder_reset=0.03)).reset == 0.03


def test_a_margin_cap_never_raises_the_ladder_leverage_past_its_liquidation_buffer():
    from ptbot.strategy import plan_ladder
    for one_wallet, free in ((True, {"A": 110.0, "B": 0.0}), (False, {"A": 300.0, "B": 300.0})):
        p = Params(strategy="ladder", one_wallet=one_wallet, max_margin_usd=10)
        plan = plan_ladder(free, 10, 1000, 18.9, p)
        assert plan.leverage <= 200 and 1 / plan.leverage - 0.0005 - plan.close_move >= 0.0025 - 1e-9


def test_choose_plan_refuses_a_pair_that_costs_more_than_the_cap():
    from ptbot.strategy import choose_plan
    pair = Params(strategy="pair", leverage=250, max_leverage=250, close_move=0.007)   # x250: exit at 0.20%
    plan, _ = choose_plan({"A": 50.0, "B": 50.0}, 10, 1000, 18.9, pair)
    assert isinstance(plan, Skip) and "plafond" in plan.reason
    x40 = Params(strategy="pair", leverage=40, max_leverage=40, close_move=0.02, target_notional=0)
    assert isinstance(choose_plan({"A": 50.0, "B": 50.0}, 10, 1000, 18.9, x40)[0], OpenPlan)
    # Too little for the ladder: a pair closed at the ladder's reset move, which costs what a period would.
    plan, note = choose_plan({"A": 30.0, "B": 30.0}, 10, 1000, 18.9, Params(strategy="ladder"))
    assert note and plan.close_move == 0.0205 and plan.leverage <= 44


def test_ladder_cost_rule_ignores_fill_noise_but_not_a_real_excess():
    import random

    from ptbot.strategy import cost_cap_hit
    p = Params(strategy="ladder", max_cost_per_1000=7.0, cost_window=3)
    rng = random.Random(1)

    def periods(mean, n):
        return [{"usd": max(0.1, rng.gauss(mean, 1.0)) * 1.15, "paper": 1150.0, "mode": "ladder"} for _ in range(n)]
    noisy = periods(6.86, 60)
    assert not any(cost_cap_hit(noisy[:i], p)[0] for i in range(1, 61))     # ±1$/1000 per period around 6.86
    assert cost_cap_hit(periods(9.0, 5), p)[0]                               # emission misread, fees changed...


def test_a_pair_after_ladder_periods_is_judged_on_pairs_only():
    p = Params(max_cost_per_1000=7.0, cost_window=3)
    ladder = [{"usd": u, "paper": 1000.0, "mode": "ladder"} for u in (8.5, 7.9, 6.0)]   # noisy, avg 7.47
    hit, avg = cost_cap_hit(ladder + [{"usd": 6.8, "paper": 1000.0, "mode": "pair"}], p)
    assert not hit and abs(avg - 6.8) < 1e-9


def test_a_fixed_ladder_reset_above_the_cap_is_refused():
    from ptbot.strategy import plan_ladder
    p = Params(strategy="ladder", one_wallet=True, ladder_reset=0.01)          # +1%: ~7.35$/1000 at E=18.9
    plan = plan_ladder({"A": 110.0, "B": 0.0}, 10, 1000, 18.9, p)
    assert isinstance(plan, Skip) and not plan.capital and "plafond" in plan.reason
    p75 = Params(strategy="ladder", one_wallet=True, ladder_reset=0.01, max_cost_per_1000=7.5)
    assert plan_ladder({"A": 110.0, "B": 0.0}, 10, 1000, 18.9, p75).reset == 0.01
