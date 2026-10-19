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
    assert venue.calls["close", "A"] == 3 + 1                       # both legs of the reset in one Close
    assert venue.liquidations == 0


class RefuseReopens(FaultVenue):
    """The first short opens, every later short is refused (open-interest cap)."""

    def open(self, w, coin, is_long, margin, leverage, deadline_s):
        if not is_long and self.calls["open", w, "short"]:
            self.calls["open", w, "short"] += 1
            raise IntentRejected("openPosition refusé (HTTP 409): {'code': 'admission_rejected', 'reason': 'openInterest'}")
        self.calls["open", w, "short" if not is_long else "long"] += 1
        return super().open(w, coin, is_long, margin, leverage, deadline_s)


def test_a_refused_reopen_keeps_the_old_leg_until_it_nears_liquidation(tmp_path):
    points = UP[:2] + [(240, 100_400)]       # the short: -0.26% (refused re-opens), then -0.40%
    venue, store, bot = make(tmp_path, points, ladder(), venue_cls=RefuseReopens)
    seen = track_positions(venue)
    run_until(bot, 230)
    assert venue.calls["open", "B", "short"] > 1 + bot.t.rearm_attempts      # retried, never given up
    assert len(venue.accounts["A"].positions) == 1 and len(venue.accounts["B"].positions) == 1   # still hedged
    assert venue.calls["close", "B"] == 0 and len(seen["B"]) == 1
    while store.data["totals"]["cycles"] == 0 and venue.now() - T0 < 600:
        bot.run(max_steps=1)
    st = store.data
    assert st["cycles_all"][0]["kind"] == "cut" and not st["history"]      # outside the per-cycle cost cap
    assert not venue.accounts["A"].positions and not venue.accounts["B"].positions and venue.liquidations == 0


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


def test_crash_while_reopening_keeps_the_old_leg_until_the_order_expires(tmp_path):
    venue, store, bot = make(tmp_path, UP[:2], ladder(), venue_cls=CrashOnReopen)
    seen = track_positions(venue)
    try:
        run_until(bot, 200)
    except KeyboardInterrupt:
        pass
    assert store.data["phase"] == "rearming" and venue.calls["close", "B"] == 0   # died before closing the old leg
    bot2 = Bot(venue, ladder(), StateStore(tmp_path / "state.json"), Timing())
    t = venue.now() - T0
    run_until(bot2, t + 200)                 # the re-open may still execute: the old short keeps hedging
    assert venue.calls["close", "B"] == 0 and len(venue.accounts["B"].positions) == 1
    run_until(bot2, t + 400)                 # it expired: re-armed again
    st = bot2.st
    assert st["phase"] == "open" and st["cycle"]["rearms"] == 1 and not st["cycles_all"]
    assert len(seen["B"]) == 2 and venue.max_positions <= 2


def test_rearm_stops_when_the_losing_wallet_cannot_afford_it(tmp_path):
    venue, store, bot = make(tmp_path, UP, ladder(ladder_reset=0.03))
    run_until(bot, 50)
    assert store.data["phase"] == "open"
    venue.accounts["B"].usdc = 3.0          # e.g. spent elsewhere: the next step cannot be paid for
    while store.data["totals"]["cycles"] == 0 and venue.now() - T0 < 400:
        bot.run(max_steps=1)
    h = store.data["cycles_all"][0]
    assert h["rearms"] == 0 and h["kind"] == "cut" and venue.calls["open", "B"] == 1
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
    assert venue.order == ["long"]                       # nothing opened: tried again 10 minutes later
    run_until(bot, 800)
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


def test_an_unreadable_balance_does_not_hold_back_the_losing_leg(tmp_path):
    class RpcDown(FaultVenue):
        rpc_down = False

        def usdc(self, w):
            if self.rpc_down:
                raise RuntimeError("HyperEVM RPC timeout")
            return super().usdc(w)

    points = [(0, 100_000), (100, 99_740), (130, 99_650), (160, 99_560), (190, 99_400)]
    venue, store, bot = make(tmp_path, points, ladder(), venue_cls=RpcDown)
    run_until(bot, 60)
    venue.rpc_down = True
    run_until(bot, 240)
    venue.positions("A")                     # the simulator liquidates on reads
    assert venue.liquidations == 0
    assert venue.calls["close", "A"] == 2 and store.data["cycle"]["rearms"] == 2


def test_a_short_open_interest_refusal_only_delays_the_rearm(tmp_path):
    class RefuseFor30s(FaultVenue):
        def open(self, w, coin, is_long, margin, leverage, deadline_s):
            if T0 + 59 <= self.clock < T0 + 89:
                raise IntentRejected("openPosition refusé (HTTP 409): {'reason': 'openInterest'}")
            return super().open(w, coin, is_long, margin, leverage, deadline_s)

    venue = RefuseFor30s(Path([(0, 100_000), (60, 100_260)]), start_usdc=55.0, emission=18.9, min_notional=1000.0)
    store = StateStore(tmp_path / "state.json")
    bot = Bot(venue, Params(), store, Timing())
    run_until(bot, 400)
    st = store.data
    assert not st["cycles_all"] and st["phase"] == "open" and st["cycle"]["rearms"] == 1


def test_a_cut_period_does_not_pause_the_farm(tmp_path):
    venue = FaultVenue(Path([(0, 100_000), (60, 100_210)]), start_usdc=110.0, emission=18.9, min_notional=1000.0)
    store = StateStore(tmp_path / "state.json")
    bot = Bot(venue, Params(), store, Timing())
    run_until(bot, 50)
    venue.accounts["B"].usdc = 3.0           # the short's wallet cannot pay for its re-open
    run_until(bot, 300)
    st = store.data
    assert [c["kind"] for c in st["cycles_all"]] == ["cut"] and not st["history"]
    assert st["cycles_all"][0]["usd"] / st["cycles_all"][0]["paper"] * 1000 > 7     # expensive, as cut periods are
    assert not st.get("paused")


def test_the_old_leg_is_closed_only_once_its_replacement_is_accepted(tmp_path):
    order = []

    class Log(FaultVenue):
        def open(self, w, coin, is_long, margin, leverage, deadline_s):
            order.append(("open", "long" if is_long else "short"))
            return super().open(w, coin, is_long, margin, leverage, deadline_s)

        def close(self, w, ids):
            order.append(("close", tuple(ids)))
            return super().close(w, ids)

    venue, store, bot = make(tmp_path, UP[:2], ladder(), venue_cls=Log)
    run_until(bot, 120)
    assert order[2:4] == [("open", "short"), ("close", (2,))]


def test_a_new_leg_is_guarded_while_the_old_one_is_still_closing(tmp_path):
    class SlowClose(FaultVenue):
        """The old short stays listed for a while after its close (slow relayer or lagging stream)."""

        def positions(self, w):
            listed = super().positions(w)
            return listed + [p for p in getattr(self, "ghosts", []) if self.clock < self.ghost_until]

        def close(self, w, ids):
            if w == "B" and ids == [2]:
                self.ghosts = [p for p in super().positions(w) if p.id == 2]
                self.ghost_until = self.clock + 60
            return super().close(w, ids)

    points = UP[:2] + [(70, 100_560)]        # 0.30% more against the short just re-opened at 100,260
    venue, store, bot = make(tmp_path, points, ladder(), venue_cls=SlowClose)
    while store.data["totals"]["cycles"] == 0 and venue.now() - T0 < 300:
        bot.run(max_steps=1)
    assert venue.liquidations == 0 and store.data["cycles_all"][0]["kind"] == "cut"


def test_a_cycle_is_closed_before_its_session_keys_expire(tmp_path):
    class Keys(FaultVenue):
        expiry = T0 + 3 * 86400

        def keys_ready(self, margin_s=3600):
            return self.expiry - self.clock > margin_s

    venue, store, bot = make(tmp_path, [(0, 100_000)], ladder(), venue_cls=Keys)
    run_until(bot, 100)
    assert store.data["phase"] == "open"
    run_until(bot, 2 * 86400 + 900)          # nothing moves: the period would never end on its own
    st = store.data
    assert [c["kind"] for c in st["cycles_all"]] == ["cut"] and st["phase"] == "idle"
    assert not venue.accounts["A"].positions and not venue.accounts["B"].positions
    venue.expiry = venue.clock + 6 * 86400   # renewed
    run_until(bot, 2 * 86400 + 1100)
    assert st["phase"] in ("opening", "open")


def test_a_queued_reopen_keeps_the_old_leg_so_the_pair_stays_hedged(tmp_path):
    for name, points in (("retrace", UP[:2] + [(80, 100_000)]), ("flat", UP[:2])):
        venue, store, bot = make(tmp_path / name, points, ladder())
        run_until(bot, 30)
        venue.faults[("open", "B")] = {"never": True}    # accepted, waits in PaperTrade's queue, never fills
        run_until(bot, 200)
        assert len(venue.accounts["A"].positions) == 1 and len(venue.accounts["B"].positions) == 1, name
        assert venue.calls["close", "B"] == 0 and not store.data["cycles_all"], name   # hedged, nothing cut


def test_a_reopen_filled_late_from_the_queue_completes_the_rearm(tmp_path):
    venue, store, bot = make(tmp_path, UP[:2], ladder())
    run_until(bot, 30)
    venue.faults[("open", "B")] = {"delay": 90}         # the re-open waits 90 s in the queue
    run_until(bot, 140)
    assert venue.calls["close", "B"] == 0 and store.data["phase"] == "rearming"
    run_until(bot, 220)
    st = store.data
    assert st["phase"] == "open" and st["cycle"]["rearms"] == 1 and venue.calls["close", "B"] == 1
    assert len(venue.accounts["B"].positions) == 1 and not st["cycles_all"]


def test_a_queued_reopen_ends_the_period_before_the_old_leg_nears_liquidation(tmp_path):
    points = UP[:2] + [(120, 100_400)]       # the short at -0.40%, past halfway to its liquidation (0.50%)
    venue, store, bot = make(tmp_path, points, ladder())
    run_until(bot, 30)
    venue.faults[("open", "B")] = {"never": True}
    while store.data["totals"]["cycles"] == 0 and venue.now() - T0 < 500:
        bot.run(max_steps=1)
    assert store.data["cycles_all"][0]["kind"] == "cut" and venue.liquidations == 0
    assert not venue.accounts["A"].positions and not venue.accounts["B"].positions


def test_slow_refused_reopens_still_end_the_period_before_liquidation(tmp_path):
    class SlowAndRefused(RefuseReopens):
        def usdc(self, w):
            self.clock += 7                      # a slow HyperEVM read: each refused try outlasts the retry delay
            return super().usdc(w)

    points = [(0, 100_000)] + [(60 + 10 * i, 100_250 + 10 * i) for i in range(30)]   # slides against the short
    venue, store, bot = make(tmp_path, points, ladder(), venue_cls=SlowAndRefused)
    while store.data["totals"]["cycles"] == 0 and venue.now() - T0 < 600:
        bot.run(max_steps=1)
        venue.positions("B")
    assert venue.liquidations == 0 and store.data["cycles_all"][0]["kind"] == "cut"


def test_a_rearm_saved_by_the_previous_version_resumes(tmp_path):
    points = UP[:2]
    venue, store, bot = make(tmp_path, points, ladder(), venue_cls=FaultVenue)
    venue.faults[("open", "B")] = {"delay": 6}
    while store.data["phase"] != "rearming":
        bot.run(max_steps=1)
    st = store.data
    for key in ("mid", "old_leg", "first_close_at"):   # what the previous version did not save
        st["cycle"]["rearm"].pop(key, None)
    st["schema"] = 3
    store.save()
    bot2 = Bot(venue, ladder(), StateStore(tmp_path / "state.json"), Timing())
    run_until(bot2, 200)
    assert bot2.st["phase"] == "open" and bot2.st["cycle"]["rearms"] == 1


def test_one_wallet_waits_for_the_straddle_test_after_bringing_b_over(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], ladder(one_wallet=True))
    passed = []
    bot.straddle_check = lambda: bool(passed)
    run_until(bot, 900)
    st = store.data
    assert st["paused_kind"] == "straddle" and venue.accounts["B"].usdc < 1     # B's USDC is on A for the test
    assert not venue.accounts["A"].positions
    passed.append(True)                      # test done (bot restarted)
    run_until(bot, 1000)
    assert not st["paused"] and st["phase"] == "open"


def test_a_classic_pair_keeps_the_state_readable_by_the_previous_version(tmp_path):
    from . import x100
    venue, store, bot = make(tmp_path, [(0, 100_000)], x100())
    venue.mins = (10.0, 10_000.0)
    store.data["schema"] = 3
    run_until(bot, 100)
    assert store.data["phase"] == "open" and store.data["schema"] == 3
    venue2, store2, bot2 = make(tmp_path / "ladder", [(0, 100_000)], ladder())
    store2.data["schema"] = 3
    run_until(bot2, 100)
    assert store2.data["schema"] == 4


def test_a_reset_reached_during_a_rearm_is_a_normal_period(tmp_path):
    class SlowClose(FaultVenue):
        def positions(self, w):     # the old short stays listed a while after its close
            listed = super().positions(w)
            return listed + [p for p in getattr(self, "ghosts", []) if self.clock < self.ghost_until]

        def close(self, w, ids):
            if w == "B" and ids == [2]:
                self.ghosts = [p for p in super().positions(w) if p.id == 2]
                self.ghost_until = self.clock + 60
            return super().close(w, ids)

    points = UP[:2] + [(70, 101_010)]        # the long reaches +1% (the reset) while B is being re-armed
    venue, store, bot = make(tmp_path, points, ladder(), venue_cls=SlowClose)
    while store.data["totals"]["cycles"] == 0 and venue.now() - T0 < 300:
        bot.run(max_steps=1)
    st = store.data
    assert st["cycles_all"][0]["kind"] == "normal" and st["history"]
