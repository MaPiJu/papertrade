"""Ladder strategy: the losing leg is re-armed in small steps, the winner is realised on a large move."""
from ptbot.bot import Bot, StateStore, Timing
from ptbot.papertrade_api import IntentRejected
from ptbot.strategy import Params

from .test_bot import T0, Path
from .test_faults import FaultVenue

UP = [(0, 100_000), (60, 100_260), (300, 100_520), (500, 100_780), (700, 101_040)]


def ladder(**kw) -> Params:
    return Params(**{"strategy": "ladder", "ladder_step": 0.0025, "ladder_reset": 0.01, **kw})


def make(tmp_path, points, params, usdc=100.0, venue_cls=FaultVenue, **venue_kw):
    venue = venue_cls(Path(points), start_usdc=usdc, emission=50, min_notional=1000.0, **venue_kw)
    store = StateStore(tmp_path / "state.json")
    return venue, store, Bot(venue, params, store, Timing())


def run_until(bot, t):
    while bot.v.now() - T0 < t:
        bot.run(max_steps=1)


def track_positions(venue):
    """Records every position id seen on each wallet."""
    seen = {"A": set(), "B": set()}
    real_sleep = venue.sleep

    def sleep(seconds):
        real_sleep(seconds)
        for w, acc in venue.accounts.items():
            seen[w] |= set(acc.positions)
    venue.sleep = sleep
    return seen


def test_losing_leg_is_rearmed_and_winner_cashed_at_reset(tmp_path):
    venue, store, bot = make(tmp_path, UP, ladder())
    seen = track_positions(venue)
    run_until(bot, 760)
    st = store.data
    assert st["totals"]["cycles"] == 1
    h = st["history"][0]
    assert h["mode"] == "ladder" and h["rearms"] == 3
    assert venue.calls["open", "A"] == 1 and venue.calls["open", "B"] == 4   # the long was never touched
    assert len(seen["A"]) == 1 and len(seen["B"]) == 4
    assert venue.liquidations == 0
    assert h["paper"] > 2_000             # every step minted PAPER (~590 each at E=50)
    assert 0 < h["usd"] < 15              # the haircut was paid once, on a ~1% gain


def test_one_wallet_mode_brings_b_over_and_keeps_both_legs_on_a(tmp_path):
    points = [(0, 100_000)] + [(t + 900, px) for t, px in UP[1:]]
    venue, store, bot = make(tmp_path, points, ladder(one_wallet=True))
    seen = track_positions(venue)
    run_until(bot, 1660)
    assert venue.accounts["B"].usdc < 1 and not seen["B"]          # B's USDC moved to A, B never traded
    st = store.data
    assert st["totals"]["cycles"] == 1 and st["history"][0]["rearms"] == 3
    assert venue.max_positions <= 3                                 # partner leg never swept (old+new during a swap)
    assert venue.calls["close", "A"] == 3 + 2
    assert venue.liquidations == 0


class RefuseReopens(FaultVenue):
    """The first short opens, every later short is refused (open-interest cap)."""

    def open(self, w, coin, is_long, margin, leverage, deadline_s):
        if not is_long and self.calls["open", w, "short"]:
            self.calls["open", w, "short"] += 1
            raise IntentRejected("openPosition refusé (HTTP 409): {'code': 'admission_rejected', 'reason': 'openInterest'}")
        self.calls["open", w, "short" if not is_long else "long"] += 1
        return super().open(w, coin, is_long, margin, leverage, deadline_s)


def test_refused_reopens_end_the_period_without_a_naked_leg(tmp_path):
    venue, store, bot = make(tmp_path, UP[:2], ladder(), venue_cls=RefuseReopens)
    while store.data["totals"]["cycles"] == 0 and venue.now() - T0 < 400:
        bot.run(max_steps=1)
    st = store.data
    assert venue.calls["open", "B", "short"] == 1 + bot.t.rearm_attempts
    assert st["totals"]["cycles"] == 1 and st["history"][0]["rearms"] == 0
    assert not venue.accounts["A"].positions and not venue.accounts["B"].positions   # the long was closed too


class CrashOnReopen(FaultVenue):
    def open(self, w, coin, is_long, margin, leverage, deadline_s):
        self.calls["open", w] += 1
        if w == "B" and self.calls["open", w] == 2 and not getattr(self, "crashed", False):
            self.crashed = True
            raise KeyboardInterrupt   # killed right after the state was saved, before the request
        return SimVenue_open(self, w, coin, is_long, margin, leverage, deadline_s)


def SimVenue_open(venue, *args):
    from ptbot.sim import SimVenue
    return SimVenue.open(venue, *args)


def test_crash_while_reopening_resumes_without_duplicates(tmp_path):
    venue, store, bot = make(tmp_path, UP[:2], ladder(), venue_cls=CrashOnReopen)
    seen = track_positions(venue)
    try:
        run_until(bot, 200)
    except KeyboardInterrupt:
        pass
    assert store.data["phase"] == "rearming"
    bot2 = Bot(venue, ladder(), StateStore(tmp_path / "state.json"), Timing())
    t = venue.now() - T0
    run_until(bot2, t + bot2.t.open_deadline_s + 200)
    c = bot2.st["cycle"]
    assert bot2.st["phase"] == "open" and c["rearms"] == 1
    assert len(venue.accounts["B"].positions) == 1 and len(seen["B"]) == 2
    assert venue.max_positions <= 1 + 1


def test_rearm_stops_when_the_losing_wallet_cannot_afford_it(tmp_path):
    venue, store, bot = make(tmp_path, UP, ladder(ladder_reset=0.03))
    run_until(bot, 50)
    assert store.data["phase"] == "open"
    venue.accounts["B"].usdc = 3.0          # e.g. spent elsewhere: the next step cannot be paid for
    while store.data["totals"]["cycles"] == 0 and venue.now() - T0 < 400:
        bot.run(max_steps=1)
    h = store.data["history"][0]
    assert h["rearms"] == 0 and venue.calls["open", "B"] == 1
    assert not venue.accounts["A"].positions and not venue.accounts["B"].positions
    assert venue.liquidations == 0


def test_open_interest_refusal_opens_the_other_side_first(tmp_path):
    class RefuseFirstLong(FaultVenue):
        order = []

        def open(self, w, coin, is_long, margin, leverage, deadline_s):
            self.order.append("long" if is_long else "short")
            if is_long and self.order.count("long") == 1:
                raise IntentRejected("openPosition refusé (HTTP 409): {'reason': 'openInterest'}")
            return super().open(w, coin, is_long, margin, leverage, deadline_s)

    venue, store, bot = make(tmp_path, [(0, 100_000)], ladder(), venue_cls=RefuseFirstLong)
    run_until(bot, 200)
    assert venue.order[:3] == ["long", "short", "long"]
    assert store.data["phase"] == "open"


def test_a_classic_pair_from_the_previous_version_finishes_as_a_pair(tmp_path):
    from . import x100
    venue, store, bot = make(tmp_path, [(0, 100_000), (300, 100_750)], x100())
    venue.mins = (10.0, 10_000.0)
    run_until(bot, 100)
    assert store.data["phase"] == "open"
    for key in ("mode", "reset", "rearms", "rearm", "wallets"):   # a cycle written by the older version
        store.data["cycle"].pop(key)
    store.save()
    bot2 = Bot(venue, ladder(), StateStore(tmp_path / "state.json"), Timing())
    while bot2.st["totals"]["cycles"] == 0 and venue.now() - T0 < 400:
        bot2.run(max_steps=1)
    h = bot2.st["history"][0]
    assert h["mode"] == "pair" and h["rearms"] == 0 and venue.calls["open", "B"] == 1   # closed as a pair at 0.7%


def test_rearmed_leg_keeps_the_btc_quantity_so_the_cost_does_not_depend_on_the_trend(tmp_path):
    costs = []
    for name, points in (("up", UP), ("down", [(t, 200_000 - px) for t, px in UP])):
        venue, store, bot = make(tmp_path / name, points, ladder())
        run_until(bot, 760)
        h = store.data["history"][0]
        assert h["rearms"] == 3
        costs.append(h["usd"] / h["paper"] * 1000)
        legs = [p for acc in venue.accounts.values() for p in acc.positions.values()]
        assert not legs
    assert abs(costs[0] - costs[1]) < 0.03 * costs[0], costs


def test_too_little_capital_for_the_ladder_falls_back_to_the_pair(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], ladder(), usdc=30.0)
    run_until(bot, 60)
    assert store.data["phase"] == "open" and store.data["cycle"]["mode"] == "pair"


def test_close_cycle_books_the_open_cycle_and_stops(tmp_path):
    venue, store, bot = make(tmp_path, UP[:2], ladder())
    run_until(bot, 100)
    assert store.data["phase"] == "open"
    bot._end_period(venue.now(), "fermeture demandée")
    bot.run(stop_when_idle=True)
    assert store.data["phase"] == "idle" and store.data["totals"]["cycles"] == 1
    assert not venue.accounts["A"].positions and not venue.accounts["B"].positions
