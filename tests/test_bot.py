"""End-to-end scenarios of the state machine against the offline simulator."""
from ptbot.bot import Bot, StateStore, Timing
from ptbot.constants import SCALE
from ptbot.papertrade_api import IntentRejected
from ptbot.sim import SimVenue

from . import x100

T0 = 1_800_000_000.0


class Path:
    """Piecewise-constant price: [(seconds after start, price), ...]."""

    def __init__(self, points):
        self.points = points

    def __call__(self, now):
        px = self.points[0][1]
        for t, p in self.points:
            if now - T0 >= t:
                px = p
        return px


def make(tmp_path, points, usdc=100.0, emission=50.0, params=None, venue_cls=SimVenue):
    venue = venue_cls(Path(points), start_usdc=usdc, emission=emission)
    store = StateStore(tmp_path / "state.json")
    return venue, store, Bot(venue, params or x100(), store, Timing())


def run_for(bot, seconds):
    bot.run(max_steps=int(seconds / bot.t.poll_s))


def test_cycle_closes_both_legs_before_liquidation_then_stakes(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (600, 100_400), (1200, 100_750)], usdc=150)
    run_for(bot, 1300)
    st = store.data
    assert st["totals"]["cycles"] == 1
    assert venue.liquidations == 0
    c = st["history"][0]
    # short leg lost ~0.75% x 15,000$ notional = ~112$; PAPER = loss x 98% x 50
    assert 5_000 < c["paper"] < 6_000
    assert 0 < c["usd"] < 30
    # PAPER minted on B was staked automatically
    assert venue.accounts["B"].staked_raw > 5_000 * SCALE
    assert venue.accounts["B"].paper_raw == 0


def test_stake_threshold(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)])
    venue.accounts["A"].paper_raw = 999 * SCALE
    venue.accounts["B"].paper_raw = 1000 * SCALE
    bot.stake_if_needed(venue.now(), force=True)
    assert venue.accounts["A"].staked_raw == 0
    assert venue.accounts["B"].staked_raw == 1000 * SCALE


def test_rebalance_after_cycle_equalises_wallets(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (600, 100_750), (1200, 100_750)], usdc=150)
    run_for(bot, 1200)
    a, b = venue.accounts["A"], venue.accounts["B"]
    free = {w: acc.usdc + sum(p.margin for p in acc.positions.values()) for w, acc in venue.accounts.items()}
    assert abs(free["A"] - free["B"]) < 2.0      # transfers are whole USDC: at most 2$ apart
    assert a.core_spot == 0 and b.core_spot == 0


def test_price_gap_through_liquidation_is_booked(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (600, 101_500)], usdc=150)
    run_for(bot, 700)
    assert venue.liquidations == 1          # the short was liquidated by the jump
    assert store.data["totals"]["cycles"] == 1
    assert store.data["phase"] in ("idle", "rebalancing", "opening", "open")
    assert not store.data.get("paused")


class MisreadEmission(SimVenue):
    """Plans with 25 PAPER/$ but mints 2: the realised cost is what catches it."""

    def paper_emission(self):
        return 25.0


def alternating_moves():
    """0.75% up, 0.75% down, ... every 15 minutes."""
    points = [(0, 100_000)]
    px = 100_000
    for i in range(1, 12):
        px = px * (1.0075 if i % 2 else 1 / 1.0075)
        points.append((i * 900, px))
    return points


def test_cost_cap_pauses_farming_but_keeps_staking(tmp_path):
    params = x100(max_cost_per_1000=7, cost_window=2)
    points = alternating_moves()
    venue, store, bot = make(tmp_path, points, usdc=300, emission=2, params=params, venue_cls=MisreadEmission)
    run_for(bot, 12 * 900)
    st = store.data
    assert st["paused"] and "plafond" in st["paused"]
    assert len(st["history"]) == 2
    venue.accounts["A"].paper_raw = 5000 * SCALE
    run_for(bot, 120)
    assert venue.accounts["A"].staked_raw >= 5000 * SCALE
    assert st["totals"]["cycles"] == 2      # no new cycle while paused


def test_without_a_cost_cap_the_balance_floor_stops_farming(tmp_path):
    # PAPER at ~70$/1000 (emission 2): far above any cap, yet farmed until both wallets hold 270$ or less.
    params = x100(max_cost_per_1000=0, min_balance_usd=270)
    venue, store, bot = make(tmp_path, alternating_moves(), usdc=150, emission=2, params=params)
    run_for(bot, 12 * 900)
    st = store.data
    assert st["totals"]["cycles"] == 2      # 300$ -> 284$ -> 269$
    assert st["paused"] and "MIN_BALANCE_USD" in st["paused"]
    assert 255 < sum(a.usdc for a in venue.accounts.values()) <= 270    # the last cycle ends a little under
    assert not any(a.positions for a in venue.accounts.values())
    venue.accounts["A"].paper_raw = 5000 * SCALE
    run_for(bot, 120)
    assert venue.accounts["A"].staked_raw >= 5000 * SCALE
    assert st["totals"]["cycles"] == 2      # no new cycle while paused


class RejectShortVenue(SimVenue):
    def open(self, w, *a, **k):
        if w == "B":
            raise IntentRejected("refusé")
        return super().open(w, *a, **k)


def test_one_leg_rejected_closes_the_other(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], venue_cls=RejectShortVenue)
    run_for(bot, 30)
    assert venue.accounts["A"].positions == {}
    assert venue.accounts["B"].positions == {}
    assert store.data["open_failures"] >= 1


class BlindVenue(SimVenue):
    """Positions exist but the API parser cannot see them."""

    def positions(self, w):
        return []


def test_invisible_positions_pause_instead_of_reopening(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], venue_cls=BlindVenue)
    run_for(bot, 600)
    assert store.data["paused"] and "aucune position" in store.data["paused"]
    assert len(venue.accounts["A"].positions) == 1    # never stacked a second pair


def test_restart_resumes_the_open_pair(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (900, 100_800)], usdc=150)
    run_for(bot, 300)
    assert store.data["phase"] == "open"
    store2 = StateStore(tmp_path / "state.json")      # reload from disk, new bot instance
    bot2 = Bot(venue, x100(), store2, Timing())
    run_for(bot2, 700)
    assert store2.data["totals"]["cycles"] == 1
    assert sum(len(a.positions) for a in venue.accounts.values()) == 2   # next pair, not a stacked one


def test_too_small_for_min_notional_waits(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], usdc=90)
    run_for(bot, 120)
    assert venue.accounts["A"].positions == {}
    assert store.data["totals"]["cycles"] == 0


class LostResponseVenue(SimVenue):
    """B's open executes but the HTTP response is lost (error on our side)."""

    def open(self, w, *a, **k):
        intent = super().open(w, *a, **k)
        if w == "B":
            raise RuntimeError("timeout")
        return intent


def test_leg_executed_despite_error_is_adopted(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], venue_cls=LostResponseVenue)
    run_for(bot, 30)
    assert store.data["phase"] == "open"
    assert len(venue.accounts["A"].positions) == 1 and len(venue.accounts["B"].positions) == 1


def test_a_pair_the_emission_makes_too_expensive_is_not_opened(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], usdc=150, emission=2)
    run_for(bot, 120)
    assert venue.accounts["A"].positions == {} and store.data["totals"]["cycles"] == 0
