"""Regression tests for failure paths: lost responses, late fills, stale reads,
crashes between a request and the state save, slow transfers, slow stakes."""
from collections import Counter

import pytest

from ptbot.bot import Bot, StateStore, Timing
from ptbot.constants import SCALE
from ptbot.papertrade_api import _list_payload
from ptbot.positions import Position
from ptbot.sim import SimVenue

from . import x100
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
    return venue, store, Bot(venue, params or x100(), store, Timing())


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
    store.data["known_ids"]["A"] = [99]
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
    assert store.data["paused"] and "toujours listée" in store.data["paused"]
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
    bot2 = Bot(venue, x100(), StateStore(tmp_path / "state.json"), Timing())
    run_for(bot2, 330)  # B may have been signed late: the lone A leg is price-watched until B's order expires
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
    bot2 = Bot(venue, x100(), StateStore(tmp_path / "state.json"), Timing())
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


# --- regressions from the review of the rewrite ---------------------------------

def test_late_fill_after_an_abort_is_closed_and_booked_with_the_cycle(tmp_path):
    # B's response is lost and it fills 100 s later; meanwhile A alone hits its exit and is closed
    points = [(0, 100_000), (20, 99_250), (100, 99_250), (110, 100_250)]
    venue, store, bot = make(tmp_path, points, {("open", "B"): {"lost": True, "delay": 100}}, usdc=150)
    run_for(bot, 200)
    assert venue.liquidations == 0
    assert all(not a.positions for a in venue.accounts.values())
    assert store.data["totals"]["cycles"] == 1


def test_one_empty_read_does_not_hide_a_stray(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)], usdc=150)
    venue.close_noops = 1                      # the first close of the stray is ignored
    SimVenue.open(venue, "B", "BTC", False, 20.0, 500, 120)   # a stray short already on B
    bot.step()                                 # idle sees it and tries to close it
    venue.empty_reads = 2                      # then one read of both wallets comes back empty
    run_for(bot, 30)
    assert venue.max_positions <= 1            # no pair opened on top of the stray
    assert 1 not in venue.accounts["B"].positions   # the stray was closed before the next pair


def test_unknown_entry_assumes_the_worst_price_seen(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (30, 100_500)], {("open", "B"): {"delay": 20}})
    real = venue.positions
    venue.positions = lambda w: [Position(p.id, p.coin, p.is_long) for p in real(w)]  # no entry prices
    run_for(bot, 60)
    legs = store.data["cycle"]["legs"]
    assert legs["B"]["entry"] == 100_000       # the short is measured from the lowest mid seen, not 100,500


def test_gateway_error_on_submit_is_ambiguous_not_a_refusal():
    from ptbot.papertrade_api import IntentRejected, PaperTradeApi

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body, self.text = code, body, str(body)
            self.ok = code < 400

        def json(self):
            if self._body is None:
                raise ValueError
            return self._body

    api = PaperTradeApi("http://unused")
    for code, body, refused in ((504, None, False), (502, {"error": "bad gateway"}, False), (200, None, False),
                                (400, {"error": "bad signature"}, True), (403, None, True),
                                (200, {"error": "nonce used"}, True)):
        api._request = lambda *a, **k: Resp(code, body)
        with pytest.raises(IntentRejected if refused else RuntimeError) as e:
            api.submit({"action": "openPosition"})
        assert isinstance(e.value, IntentRejected) == refused


def test_resume_file_clears_pause_and_counters_while_running(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)])
    store.data.update(paused="test", open_failures=3)
    (tmp_path / "state.resume").write_text("")
    sim_store = StateStore(tmp_path / "sim_state.json")       # a simulation in the same directory
    Bot(SimVenue(Path([(0, 100_000)])), x100(), sim_store, Timing()).step()
    assert (tmp_path / "state.resume").exists()               # ...does not eat the live bot's request
    bot.step()
    assert not store.data["paused"] and store.data["open_failures"] == 0
    assert not (tmp_path / "state.resume").exists()


def test_old_state_mid_cycle_is_refused_with_a_message(tmp_path):
    from ptbot.bot import IncompatibleState

    old = {"phase": "open", "cycle": {"legs": {}}, "rebalance": None, "paused": None, "history": [],
           "totals": {"cycles": 0, "usd_spent": 0.0, "paper_minted": 0.0, "paper_staked": 0.0}}
    (tmp_path / "state.json").write_text(__import__("json").dumps(old))
    with pytest.raises(IncompatibleState):
        Bot(SimVenue(Path([(0, 100_000)])), x100(), StateStore(tmp_path / "state.json"), Timing())


def test_ghost_position_in_closing_pauses(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (60, 100_750)])
    run_for(bot, 61)
    real = venue.positions
    ghost = store.data["cycle"]["legs"]["A"]["pos_id"]
    venue.positions = lambda w: [Position(ghost, "BTC", True)] + real(w) if w == "A" else real(w)
    run_for(bot, 300)
    assert not store.data["paused"]        # a slow relayer queue is not a ghost yet
    run_for(bot, 400)
    assert store.data["paused"] and "toujours listées" in store.data["paused"]


def test_read_outage_while_open_keeps_watching_the_price(tmp_path):
    # positions reads fail from t=10 s; the price crosses the exit at t=50 and the liquidation at t=60
    venue, store, bot = make(tmp_path, [(0, 100_000), (50, 100_720), (60, 101_000)])
    run_for(bot, 10)
    assert store.data["phase"] == "open"
    real = venue.positions

    def broken(w):
        raise RuntimeError("503")

    venue.positions = broken
    run_for(bot, 80)                       # the 10 s exit-to-liquidation window must not be missed
    venue.positions = real
    run_for(bot, 60)
    assert venue.liquidations == 0
    assert store.data["totals"]["cycles"] == 1


def test_read_outage_while_closing_still_resends_the_close(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (60, 100_720), (120, 100_850), (150, 101_000)])
    run_for(bot, 50)
    venue.close_noops = 1                  # B's (the loser's) first close is dropped by the relayer
    real = venue.positions

    def broken(w):
        raise RuntimeError("503")

    venue.positions = broken
    run_for(bot, 120)                      # the close of B must be resent blindly before liquidation
    venue.positions = real
    run_for(bot, 60)
    assert venue.liquidations == 0
    assert all(not a.positions for a in venue.accounts.values())


def test_queue_release_does_not_hide_an_invisible_leg(tmp_path):
    # positions are invisible, B is refused; A's margin goes into a position nobody sees,
    # while an earlier queued profit is released to A's free balance
    class Blind(FaultVenue):
        queued_usd = 80.0

        def queued(self, w):
            return self.queued_usd if w == "A" else 0.0

        def positions(self, w):
            return []

    venue = Blind(Path([(0, 100_000)]), faults={("open", "B"): {"never": True}}, start_usdc=150, emission=50)
    store = StateStore(tmp_path / "state.json")
    bot = Bot(venue, x100(), store, Timing())
    run_for(bot, 20)
    venue.accounts["A"].usdc += venue.queued_usd   # release: queued -> free, equity unchanged
    venue.queued_usd = 0.0
    run_for(bot, 300)
    assert store.data["paused"] and "aucune position n'est visible" in store.data["paused"]
    assert venue.max_positions <= 1


def test_manual_stake_during_a_cycle_does_not_trip_the_cap(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (60, 100_750)], usdc=150)
    venue.accounts["B"].paper_raw = 10_000 * SCALE
    store.data["stake_disabled_until"] = {"A": 1e12, "B": 1e12}     # auto-staking suspended
    run_for(bot, 30)
    SimVenue.stake(venue, "B", 10_000 * SCALE)                        # the user stakes by hand mid-cycle
    run_for(bot, 60)
    assert store.data["totals"]["cycles"] == 1
    assert not store.data["paused"]


def test_ghost_pause_lifts_itself_when_the_position_finally_disappears(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (60, 100_750)])
    run_for(bot, 61)
    real = venue.positions
    ghost = store.data["cycle"]["legs"]["A"]["pos_id"]
    venue.positions = lambda w: [Position(ghost, "BTC", True)] + real(w) if w == "A" else real(w)
    run_for(bot, 700)
    assert store.data["paused"]
    venue.positions = real                 # the indexer catches up
    run_for(bot, 60)
    assert not store.data["paused"]
    assert store.data["totals"]["cycles"] == 1


def test_reset_costs_forgets_the_cumulative_baseline():
    from ptbot.bot import apply_resume

    st = {"history": [1], "cycles_all": [1], "equity_first": 300.0, "strays": {}}
    apply_resume(st, reset_costs=True)
    assert st["history"] == [] and st["cycles_all"] == [] and "equity_first" not in st


# --- live-realism findings --------------------------------------------------------

def test_a_late_fill_of_another_size_is_never_adopted_as_a_leg(tmp_path):
    # B's own order fills 30 s late; meanwhile an earlier, differently sized order fills on B
    venue, store, bot = make(tmp_path, [(0, 100_000)], {("open", "B"): {"delay": 30}}, usdc=200,
                             params=x100(max_margin_usd=100))
    bot.step()                                                    # the pair's orders are sent
    assert store.data["phase"] == "opening"
    SimVenue.open(venue, "B", "BTC", False, 60.0, 200, 120)      # the earlier order's late fill
    stray = max(venue.accounts["B"].positions)
    run_for(bot, 60)
    leg_b = store.data["cycle"]["legs"]["B"]
    assert leg_b["pos_id"] != stray                              # not adopted as the cycle's short
    assert venue.accounts["B"].positions[leg_b["pos_id"]].margin == 100
    assert stray not in venue.accounts["B"].positions             # closed as a stray


def test_cycle_is_not_booked_while_a_stray_is_still_open(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000), (60, 100_750)], usdc=200, params=x100(max_margin_usd=150))
    run_for(bot, 55)
    SimVenue.open(venue, "A", "BTC", True, 30.0, 400, 120)        # an unmanaged position appears
    venue.close_noops = 0
    real_close = venue.close
    stray_id = max(venue.accounts["A"].positions)

    def slow_close(w, ids):                                       # the stray takes a while to close
        if stray_id in ids and venue.now() < 1_800_000_000 + 200:
            return "queued"
        return real_close(w, ids)

    venue.close = slow_close
    run_for(bot, 120)
    assert store.data["totals"]["cycles"] == 0                    # waits: the stray's margin is not cycle cost
    run_for(bot, 200)
    assert store.data["totals"]["cycles"] == 1
    assert not store.data.get("paused")


def test_pending_orders_are_cancelled_when_a_pair_is_abandoned(tmp_path):
    cancelled = []

    class Cancelling(FaultVenue):
        def cancel(self, w, intent_id):
            cancelled.append((w, intent_id))

    venue = Cancelling(Path([(0, 100_000), (20, 99_250)]), faults={("open", "B"): {"never": True}},
                       start_usdc=100, emission=50)
    store = StateStore(tmp_path / "state.json")
    bot = Bot(venue, x100(), store, Timing())
    run_for(bot, 40)                                              # A alone hits its exit: pair abandoned
    assert cancelled == [("B", "accepted")]


def test_orders_that_never_execute_pause_at_once(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)],
                             {("open", "A"): {"never": True}, ("open", "B"): {"never": True}})
    run_for(bot, 300)
    assert store.data["paused"] and "jamais exécutés" in store.data["paused"]
    assert venue.calls["open", "A"] == 1


def test_oldest_first_feed_is_followed_to_its_end_and_cached():
    from ptbot.papertrade_api import PaperTradeApi

    events = [{"positionId": i // 2, "type": "open" if i % 2 == 0 else "close", "timestamp": 1000 + i}
              for i in range(70)] + [{"positionId": 99, "type": "open", "isLong": True, "timestamp": 2000}]
    pages = [events[i:i + 20] for i in range(0, len(events), 20)]
    calls = []

    api = PaperTradeApi("http://unused")

    def fake_get(path, params=None, timeout=None, retry=True):
        n = int((params or {}).get("cursor") or 0)
        calls.append(n)
        return {"items": pages[n], "nextCursor": str(n + 1) if n + 1 < len(pages) else None}

    api.get = fake_get
    from ptbot.positions import open_positions
    assert [p.id for p in open_positions(api.account_trades("0xabc"))] == [99]   # page 4 of 4 is read
    assert api.trades_order("0xabc") == "asc"
    calls.clear()
    api.account_trades("0xabc")
    assert calls == [3]                                           # full pages come from the cache


def test_a_misread_margin_unit_does_not_make_the_bot_close_its_own_leg(tmp_path):
    venue, store, bot = make(tmp_path, [(0, 100_000)])
    real = venue.positions
    venue.positions = lambda w: [Position(p.id, p.coin, p.is_long, p.margin * 1e6, p.leverage, p.entry)
                                 for p in real(w)]              # margin read in the wrong unit
    run_for(bot, 30)
    assert store.data["phase"] == "open"
    assert all(len(a.positions) == 1 for a in venue.accounts.values())


def test_first_withdrawal_activation_fee_still_counts_as_arrived():
    from ptbot.bot import _arrived
    assert _arrived(10.32, 11.32)            # 1 USDC HyperCore activation on a wallet's first withdrawal
    assert not _arrived(9.0, 11.32)
    assert not _arrived(0, 11.32)
