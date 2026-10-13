"""Regression tests for failure paths: lost responses, late fills, stale reads,
crashes between a request and the state save, slow transfers, slow stakes."""
from collections import Counter

import pytest

from ptbot.bot import Bot, StateStore, Timing
from ptbot.constants import SCALE
from ptbot.papertrade_api import _list_payload
from ptbot.positions import Position
from ptbot.sim import SimVenue
from ptbot.strategy import Params

from .test_bot import Path


class FaultVenue(SimVenue):
    """SimVenue whose requests can be lost, delayed, ignored or crash mid-way.
    faults[("open", w)] = {"delay": s, "lost": bool, "never": bool, "crash": bool}"""

    def __init__(self, price_fn, faults=None, **kw):
        super().__init__(price_fn, **kw)
        self.faults = faults or {}
        self.later = []
        self.calls = Counter()
        self.empty_reads = 0
        self.close_noops = 0
        self.max_positions = 0

    def _run_due(self):
        due = sorted((x for x in self.later if x[0] <= self.clock), key=lambda x: x[0])
        self.later = [x for x in self.later if x[0] > self.clock]
        for _, fn in due:
            fn()

    def now(self):
        self._run_due()
        return super().now()

    def sleep(self, seconds):
        super().sleep(seconds)
        self._run_due()
        self.max_positions = max([self.max_positions] + [len(a.positions) for a in self.accounts.values()])

    def positions(self, w):
        self._run_due()
        if self.empty_reads > 0:
            self.empty_reads -= 1
            return []
        return super().positions(w)

    def open(self, w, coin, is_long, margin, leverage, deadline_s):
        self.calls["open", w] += 1
        f = self.faults.get(("open", w), {})
        if f.get("crash"):
            raise KeyboardInterrupt
        deadline = self.clock + deadline_s
        if f.get("never"):
            if f.get("lost"):
                raise RuntimeError("timeout")
            return "accepted"

        def execute():
            if self.clock <= deadline:
                SimVenue.open(self, w, coin, is_long, margin, leverage, deadline_s)

        if f.get("delay"):
            self.later.append((self.clock + f["delay"], execute))
        else:
            execute()
        if f.get("lost"):
            raise RuntimeError("timeout (exécuté quand même)")
        return "ok"

    def close(self, w, ids):
        self.calls["close", w] += 1
        if self.close_noops > 0:
            self.close_noops -= 1
            return "ignored"
        return super().close(w, ids)

    def withdraw_to_core(self, w, amount):
        self.calls["withdraw", w] += 1
        f = self.faults.get("withdraw", {})
        self.accounts[w].usdc -= amount
        self.later.append((self.clock + f.get("delay", 0), lambda: self._credit_core(w, amount)))
        if f.get("crash"):
            raise KeyboardInterrupt
        return "ok"

    def _credit_core(self, w, amount):
        self.accounts[w].core_spot += amount

    def core_spot_send(self, w, destination, amount):
        self.calls["send", w] += 1
        dst = destination.rsplit("-", 1)[1]
        self.accounts[w].core_spot -= amount
        delay = self.faults.get("send", {}).get("delay", 0)
        self.later.append((self.clock + delay, lambda: setattr(self.accounts[dst], "usdc", self.accounts[dst].usdc + amount)))

    def stake(self, w, amount_raw):
        self.calls["stake", w] += 1
        delay = self.faults.get("stake", {}).get("delay", 0)
        self.later.append((self.clock + delay, lambda: SimVenue.stake(self, w, amount_raw)))
        return "ok"


def make(tmp_path, points, faults=None, usdc=100.0, params=None):
    venue = FaultVenue(Path(points), faults=faults, start_usdc=usdc, emission=50)
    store = StateStore(tmp_path / "state.json")
    return venue, store, Bot(venue, params or Params(), store, Timing())


def run_for(bot, seconds):
    bot.run(max_steps=int(seconds / bot.t.poll_s))


def legs_open(venue):
    return {w: list(a.positions) for w, a in venue.accounts.items()}


def test_lost_response_on_a_that_executed_still_forms_a_pair(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], {("open", "A"): {"lost": True}})
    run_for(bot, 30)
    assert store.data["phase"] == "open"
    assert all(len(ids) == 1 for ids in legs_open(venue).values())


def test_lost_response_on_b_waits_for_the_late_fill_instead_of_closing_a(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], {("open", "B"): {"lost": True, "delay": 60}})
    run_for(bot, 100)
    assert store.data["phase"] == "open"
    assert all(len(ids) == 1 for ids in legs_open(venue).values())
    assert venue.calls["close", "A"] == 0


def test_order_that_never_executes_closes_the_lone_leg_and_never_stacks(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], {("open", "B"): {"never": True}}, usdc=250)
    run_for(bot, 1200)
    assert venue.max_positions <= 1
    assert venue.calls["close", "A"] >= 1
    assert store.data["paused"] and "3 paires ratées" in store.data["paused"]


def test_lone_leg_is_closed_at_its_exit_before_the_deadline(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (30, 99_200)], {("open", "B"): {"never": True}})
    run_for(bot, 60)
    assert venue.accounts["A"].positions == {}
    assert venue.liquidations == 0
    assert not store.data.get("paused")


def test_exit_uses_each_legs_own_entry(tmp_path):
    # B fills 50 s late, 0.5% lower: the averaged entry would let A reach liquidation
    points = [(0, 100_000), (40, 99_500), (100, 99_250), (110, 99_000)]
    venue, store, bot = make(tmp_path, points, {("open", "B"): {"delay": 50}})
    run_for(bot, 140)
    assert venue.liquidations == 0
    assert store.data["totals"]["cycles"] == 1


def test_previously_seen_position_is_never_adopted(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)])
    store.data["seen_ids"]["A"] = [99]
    real = venue.positions

    def lagging(w):  # while opening, the API still lists an old closed position on A
        ghost = [Position(99, "BTC", True)] if w == "A" and store.data["phase"] == "opening" else []
        return ghost + real(w)

    venue.positions = lagging
    run_for(bot, 20)
    assert store.data["cycle"]["legs"]["A"]["pos_id"] != 99


def test_ghost_position_pauses_instead_of_blocking_silently(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)])
    real = venue.positions
    venue.positions = lambda w: [Position(7, "BTC", True)] + real(w) if w == "A" else real(w)
    run_for(bot, 300)
    assert store.data["paused"] and "toujours listées" in store.data["paused"]
    assert venue.max_positions == 0


def test_single_empty_read_does_not_close_the_pair(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)])
    run_for(bot, 20)
    assert store.data["phase"] == "open"
    venue.empty_reads = 2
    run_for(bot, 60)
    assert store.data["phase"] == "open"
    assert venue.calls["close", "A"] == venue.calls["close", "B"] == 0


def test_silently_ignored_close_is_resent_quickly(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (60, 100_720)])
    run_for(bot, 50)
    venue.close_noops = 2
    run_for(bot, 60)
    assert store.data["totals"]["cycles"] == 1
    assert venue.liquidations == 0


def test_slow_transfers_complete_without_pause(tmp_path):
    faults = {"withdraw": {"delay": 20 * 60}, "send": {"delay": 25 * 60}}
    venue, store, bot = make(tmp_path, [(0, 100_000), (60, 100_750)], faults, usdc=150)
    run_for(bot, 2 * 3600)
    assert not store.data.get("paused")
    assert venue.calls["withdraw", "A"] == 1 and venue.calls["send", "A"] == 1
    assert venue.accounts["A"].core_spot == 0


def test_crash_between_open_and_save_resumes_and_closes_the_lone_leg(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], {("open", "B"): {"crash": True}}, usdc=150)
    with pytest.raises(KeyboardInterrupt):
        bot.step()
    assert len(venue.accounts["A"].positions) == 1
    venue.faults = {}
    bot2 = Bot(venue, Params(), StateStore(tmp_path / "state.json"), Timing())
    run_for(bot2, 200)
    assert venue.max_positions <= 1
    assert bot2.st["totals"]["cycles"] == 1          # the aborted lone leg was closed and booked


def test_crash_after_withdraw_does_not_withdraw_twice(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (60, 100_750)], usdc=150)
    for _ in range(200):
        if store.data["phase"] == "rebalancing":
            break
        bot.step()
        venue.sleep(2)
    assert store.data["phase"] == "rebalancing"
    venue.faults = {"withdraw": {"crash": True}}
    with pytest.raises(KeyboardInterrupt):
        for _ in range(20):
            bot.step()
            venue.sleep(2)
    venue.faults = {}
    bot2 = Bot(venue, Params(), StateStore(tmp_path / "state.json"), Timing())
    run_for(bot2, 120)
    assert venue.calls["withdraw", "A"] == 1
    assert venue.accounts["A"].core_spot == 0
    assert abs(venue.accounts["A"].usdc - venue.accounts["B"].usdc) < 1 or bot2.st["phase"] != "idle"


def test_slow_stake_is_sent_once_and_counted_once(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], {"stake": {"delay": 90}})
    venue.accounts["A"].paper_raw = 2000 * SCALE
    run_for(bot, 200)
    assert venue.calls["stake", "A"] == 1
    assert venue.accounts["A"].staked_raw == 2000 * SCALE
    assert store.data["totals"]["paper_staked"] == 2000


def test_unknown_api_payload_raises_instead_of_reading_no_positions():
    assert _list_payload({"trades": [{"positionId": 1}]}) == [{"positionId": 1}]
    assert _list_payload([1]) == [1]
    with pytest.raises(ValueError):
        _list_payload({"ok": True, "result": {}})
