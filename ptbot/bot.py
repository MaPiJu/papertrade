"""Delta-neutral state machine: idle -> opening -> open -> closing -> idle,
with a `rebalancing` detour when the two USDC balances drift apart.

Two strategies share it. "pair": both legs are closed together once the price has moved
`close_move`. "ladder": the losing leg is closed after `close_move` (minting PAPER) and
re-opened at once (`rearming`), while the winning leg keeps running; the period ends when a
leg's gain reaches `reset`, so the profit, which carries the haircut, is realised on a large
move, at a low cost per PAPER, while PAPER is minted in small steps.

Safety rules the code below relies on:
  - an order whose submit failed ambiguously (timeout, 5xx, lost response) may
    still execute until its signed deadline: the bot keeps watching for it,
    books it with its cycle, and never opens a new pair before that deadline;
  - every positions read closes whatever is not a leg of the current cycle
    (late fill, ghost, manual trade): the wallets are dedicated to the bot. A
    position counts as gone only after several consecutive reads without it;
  - handlers read everything first and change the state afterwards, and the
    state is saved before every irreversible request, so an error or a crash
    resumes where it was instead of acting twice or losing track of a position.
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path

from .constants import SCALE, SESSION_KEY_RENEW_S, STAKE_DEADLINE_S, WITHDRAW_DEADLINE_S
from .papertrade_api import IntentRejected
from .positions import Position, plausible_price
from .strategy import (MIN_CORE_TRANSFER_USD, MIN_DEPOSIT_USD, Params, Skip, Transfer, choose_plan, cost_cap_hit,
                       cost_per_1000, floor_cents, ladder_cost_hit, liq_distance, plan_rebalance)

log = logging.getLogger("ptbot")

WALLETS = ("A", "B")                # the two wallets of .env
LEGS = ("A", "B")                   # leg A is the long, leg B the short: on wallets A and B, or both on A
IS_LONG = {"A": True, "B": False}
# Leg statuses whose order may still execute ('pending' = state saved before the request, then a crash).
MAYBE_EXECUTED = ("sent", "unknown", "pending")
# A cycle is closed early when a session key gets this close to expiry (keys are renewed between
# cycles, SESSION_KEY_RENEW_S before expiry), so its closes can always be signed.
KEY_MIN_LEFT_S = SESSION_KEY_RENEW_S / 2
# 4: ladder cycles (rearming phase, both legs on one wallet). A schema-3 file (classic pairs) is read
# as is and stays 3 until a ladder or one-wallet cycle is written, so the previous version can still
# take it back.
SCHEMA = 4


class IncompatibleState(RuntimeError):
    pass


@dataclass
class Timing:
    poll_s: float = 2.0              # main loop period
    positions_every_s: float = 10.0  # leg checks while the pair is open
    fast_poll_s: float = 3.0         # polling while opening/closing, waiting on intents or strays
    missing_reads: int = 3           # consecutive reads without a position before it counts as gone
    stake_every_s: float = 30.0
    open_deadline_s: int = 120       # signed open intents expire after this
    indexer_slack_s: float = 30.0    # time for an executed order to show up in the API
    submit_margin_s: float = 120.0   # bound on how long submitting both opens can take (crash recovery)
    close_retry_s: float = 12.0
    ghost_retry_s: float = 60.0
    max_close_sends: int = 8         # closes of one position before pausing for a human...
    ghost_after_s: float = 600.0     # ...and only if it is still listed this long after the first close
    key_check_s: float = 600.0       # session-key renewal check, between cycles only
    stake_track_s: float = 10.0
    settle_s: float = 5.0
    idle_retry_s: float = 60.0
    stray_retry_s: float = 15.0
    stake_disable_s: float = 6 * 3600.0
    rebalance_poll_s: float = 10.0
    unsent_after_s: float = 90.0
    rebalance_warn_s: float = 900.0
    rebalance_give_up_s: float = 7200.0
    heartbeat_s: float = 60.0
    rearm_attempts: int = 3          # ladder: re-opens of a leg before ending the period
    rearm_retry_s: float = 8.0
    unfilled_retry_s: float = 600.0  # orders accepted but never filled (PaperTrade's queue full): retry after
    unfilled_pause_after: int = 6    # ...and pause after this many in a row
    rearm_naked_s: float = 60.0      # ladder: longest wait for a re-opened leg after the old one's close
                                     # (half of open_deadline_s when that is longer)


def fresh_state() -> dict:
    return {"schema": SCHEMA, "phase": "idle", "cycle": None, "rebalance": None, "paused": None,
            "history": [], "cycles_all": [],
            "totals": {"cycles": 0, "usd_spent": 0.0, "paper_minted": 0.0, "paper_staked": 0.0,
                       "usd_offcycle": 0.0}}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return fresh_state()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)


def apply_resume(st: dict, reset_costs: bool = False) -> None:
    """Clears a pause and the failure counters that lead to one."""
    st.update(paused=None, paused_kind=None, next_try=0, open_failures=0, stake_failures={},
              stake_disabled_until={})
    for s in st.get("strays", {}).values():
        s["attempts"] = 0
    if reset_costs:
        st["history"] = []
        st["cycles_all"] = []
        st.pop("equity_first", None)


def _arrived(delta: float, amount: float) -> bool:
    """A transfer counts as arrived net of small fees, including the 1 USDC HyperCore activation
    of a wallet's first withdrawal (and never when nothing came)."""
    return delta > 0 and delta >= amount - max(1.05, min(1.5, 0.05 * amount))


class Bot:
    def __init__(self, venue, params: Params, store: StateStore, timing: Timing | None = None) -> None:
        self.v = venue
        self.p = params
        self.store = store
        self.st = store.data
        self.t = timing or Timing()
        self._last_stake_check = 0.0
        self._last_idle_msg = ""
        self._stop = False
        # ONE_WALLET: set by `run` to the verdict of the long + short test on wallet A, checked before
        # the first one-wallet cycle (after B's USDC has been brought to A, which the test needs).
        self.straddle_check = None
        self._migrate()

    def _migrate(self) -> None:
        if self.st.get("schema") not in (SCHEMA, 3):
            if self.st.get("phase", "idle") != "idle" or self.st.get("cycle") or self.st.get("rebalance"):
                raise IncompatibleState(
                    f"{self.store.path} vient d'une ancienne version du bot et un cycle ou un transfert est en "
                    "cours. Vérifiez sur le site, lancez `python -m ptbot close-all` si besoin, terminez un "
                    "éventuel transfert à la main, puis supprimez ce fichier avant de relancer.")
            self.st["schema"] = SCHEMA
        for key, default in fresh_state().items():
            self.st.setdefault(key, default)
        for key, default in fresh_state()["totals"].items():
            self.st["totals"].setdefault(key, default)
        for key, default in (("known_ids", {}), ("strays", {}), ("pending_stakes", {}), ("stake_failures", {}),
                             ("stake_disabled_until", {}), ("open_failures", 0), ("intents_valid_until", 0)):
            self.st.setdefault(key, default)

    # --- main loop -------------------------------------------------------
    def run(self, max_steps: int | None = None, stop_file: Path | None = None, stop_when_idle: bool = False) -> None:
        restore = self._catch_signals()
        steps = errors = 0
        try:
            while (max_steps is None or steps < max_steps) and not self._stop:
                if stop_file and stop_file.exists():
                    log.warning("Fichier %s présent: arrêt du bot", stop_file)
                    return
                if stop_when_idle and self.st["phase"] == "idle":
                    return
                try:
                    self.step()
                    errors = 0
                except Exception as e:
                    errors += 1
                    self.store.save()
                    if isinstance(e, OSError):   # timeouts, refused connections (requests' errors are OSErrors)
                        log.warning("API injoignable ou trop lente (%s): nouvel essai au prochain tour", e)
                    else:
                        log.exception("Erreur pendant le cycle (nouvel essai au prochain tour)")
                    # back off while an API is down, but keep watching the price while positions may be open
                    exposed = self.st["phase"] in ("opening", "open", "closing", "rearming")
                    self.v.sleep(min(2 * errors, 6) if exposed else min(5 * errors, 60))
                self.v.sleep(self.t.poll_s)
                steps += 1
        finally:
            restore()
            self.store.save()

    def _catch_signals(self):
        """Ctrl+C / SIGTERM stop the bot between two steps, never in the middle of one."""
        if threading.current_thread() is not threading.main_thread():
            return lambda: None

        def handler(signum, frame):
            if self._stop:
                raise KeyboardInterrupt
            log.warning("Arrêt demandé: je termine l'étape en cours (Ctrl+C à nouveau pour forcer)")
            self._stop = True

        old = {s: signal.signal(s, handler) for s in (signal.SIGINT, signal.SIGTERM)}
        return lambda: [signal.signal(s, h) for s, h in old.items()]

    def step(self) -> None:
        resume_file = self.store.path and self.store.path.with_suffix(".resume")   # from `python -m ptbot resume`
        if resume_file and resume_file.exists():
            self.resume(reset_costs="reset-costs" in resume_file.read_text())
            resume_file.unlink()
        handler = {"idle": self._idle, "opening": self._opening, "open": self._open, "closing": self._closing,
                   "rearming": self._rearming, "rebalancing": self._rebalancing}[self.st["phase"]]
        handler(self.v.now())
        self.store.save()

    def pause(self, reason: str, kind: str | None = None) -> None:
        self.st["paused"] = reason
        self.st["paused_kind"] = kind
        log.error("PAUSE: %s (staking actif; `python -m ptbot resume` pour reprendre)", reason)

    def _clear_pause(self, kind: str) -> None:
        """Lifts a pause that was only about a position that has since gone away."""
        if self.st.get("paused") and self.st.get("paused_kind") == kind:
            log.info("La position en cause a disparu: fin de la pause")
            self.st["paused"] = None
            self.st["paused_kind"] = None

    def resume(self, reset_costs: bool = False) -> None:
        apply_resume(self.st, reset_costs)
        log.info("Pause levée")

    def _say_once(self, msg: str) -> None:
        if msg != self._last_idle_msg:
            log.info(msg)
            self._last_idle_msg = msg

    # --- positions bookkeeping ------------------------------------------------
    def _read(self, w: str) -> dict[int, Position]:
        return {p.id: p for p in self.v.positions(w)}

    def _known(self, w: str) -> list:
        return self.st["known_ids"].setdefault(w, [])

    def _remember(self, w: str, pid: int) -> None:
        known = self._known(w)
        if pid not in known:
            known.append(pid)
            del known[:-500]

    def _straddle_ok(self) -> bool:
        return not (self.p.one_wallet and self.straddle_check) or self.straddle_check()

    def _keys_ok(self, margin_s: float) -> bool:
        keys_ready = getattr(self.v, "keys_ready", None)
        try:
            return not keys_ready or keys_ready(margin_s=margin_s)
        except Exception as e:
            log.warning("Expiration des session keys illisible (%s)", e)
            return True

    def _wallet(self, leg: str) -> str:
        """Wallet carrying a leg of the current cycle (cycles of older versions: leg = wallet)."""
        c = self.st.get("cycle") or {}
        return c.get("wallets", {}).get(leg, leg)

    def _cycle_wallets(self) -> list[str]:
        return sorted({self._wallet(leg) for leg in LEGS})

    def _keep(self, w: str) -> set:
        """Positions of wallet `w` that belong to the cycle (both legs when they share it)."""
        c = self.st.get("cycle")
        if not c:
            return set()
        keep = {c["legs"][leg]["pos_id"] for leg in LEGS if self._wallet(leg) == w}
        r = c.get("rearm")
        if r and r.get("old_id") is not None and self._wallet(r["leg"]) == w:
            keep.add(r["old_id"])
        return keep - {None}

    def _sweep(self, w: str, listed: dict, keep: set, now: float) -> None:
        """Closes every listed position of `w` that is not in `keep`, and forgets a
        stray only after it has been missing for `missing_reads` reads in a row."""
        strays = self.st["strays"]
        for pid in set(listed) - keep:
            self._remember(w, pid)
            s = strays.setdefault(f"{w}:{pid}", {"w": w, "id": pid, "attempts": 0, "misses": 0, "last_close": 0})
            s["misses"] = 0
            if now - s["last_close"] < self.t.stray_retry_s:
                continue
            s["attempts"] += 1
            s["last_close"] = now
            if s["attempts"] <= 3 or s["attempts"] % 20 == 0:
                log.warning("Wallet %s: position #%s non gérée, je la ferme (wallets dédiés au bot)", w, pid)
            if s["attempts"] == self.t.max_close_sends:
                self.pause(f"wallet {w}: position #{pid} toujours listée après {s['attempts']} fermetures. Si le "
                           "site la montre fermée, l'API est mal lue (`python -m ptbot probe`); sinon fermez-la "
                           "à la main", kind=f"stray:{w}:{pid}")
            try:
                self.v.close(w, [pid])
            except Exception as e:
                log.error("Fermeture de #%s sur %s refusée: %s", pid, w, e)
        for key, s in list(strays.items()):
            if s["w"] == w and s["id"] not in listed:
                s["misses"] += 1
                if s["misses"] >= self.t.missing_reads:
                    log.info("Wallet %s: position #%s fermée", w, s["id"])
                    del strays[key]
                    self._clear_pause(f"stray:{key}")

    # --- idle: stake, clean up, rebalance, open the next pair ----------------
    def _idle(self, now: float) -> None:
        self._track_stakes(now)
        self.stake_if_needed(now)
        if now < self.st.get("next_try", 0):
            return
        listed = {w: self._read(w) for w in WALLETS}
        for w in WALLETS:
            self._sweep(w, listed[w], set(), now)
        waiting = now < self.st["intents_valid_until"]
        if self.st["strays"] or waiting:
            # an order of the last cycle may still execute: watch closely until it expires
            self.st["next_try"] = now + self.t.fast_poll_s
            if waiting:
                self._say_once("J'attends l'expiration des ordres du cycle précédent avant d'en ouvrir d'autres")
            return
        self.st["next_try"] = now + self.t.idle_retry_s
        maintain = getattr(self.v, "maintain_keys", None)
        if maintain and now >= self.st.get("next_key_check", 0):
            self.st["next_key_check"] = now + self.t.key_check_s
            maintain()   # no pair is open here: a renewal can never leave a close unsigned
        if not self._keys_ok(KEY_MIN_LEFT_S):
            self.st["next_key_check"] = 0   # renewal refused or not answered: try again next round
            self._say_once("Session keys bientôt expirées et pas encore renouvelées: pas de nouveau cycle")
            return
        if self.st.get("paused_kind") == "straddle" and self._straddle_ok():
            self.st.update(paused=None, paused_kind=None)
            log.info("Test long + short du wallet A réussi: fin de la pause")
        if self.st.get("paused"):
            self._say_once(f"En pause: {self.st['paused']}")
            return
        if self.v.trading_paused():
            self._say_once("Trading en pause côté PaperTrade, j'attends")
            return
        if self.st["pending_stakes"]:
            self.st["next_try"] = now + self.t.fast_poll_s
            return
        bal = {w: self.v.usdc(w) for w in WALLETS}
        mins = self.v.minimums()
        if self.p.one_wallet:
            # Everything trades from wallet A: bring B's USDC over (a transfer needs 11 USDC).
            amount = float(math.floor(bal["B"]))
            transfer = Transfer("B", "A", amount) if amount >= MIN_CORE_TRANSFER_USD and not listed["B"] else None
        else:
            transfer = plan_rebalance(bal["A"], bal["B"], self.p, mins)
        if transfer:
            log.info("Soldes déséquilibrés (A=%.2f$, B=%.2f$): transfert de %.2f$ de %s vers %s",
                     bal["A"], bal["B"], transfer.amount, transfer.src, transfer.dst)
            self.st["rebalance"] = {"src": transfer.src, "dst": transfer.dst, "amount": transfer.amount,
                                    "step": "withdraw", "step_started": now, "warned": now, "next_poll": 0,
                                    "withdraw_attempts": 0, "send_attempts": 0}
            self.st["phase"] = "rebalancing"
            return
        if not self._straddle_ok():
            self.pause("ONE_WALLET: test long + short sur le wallet A jamais fait. Arrêtez le bot (fichier STOP), "
                       "lancez `python tools/preflight_tiny_trade.py --wallet A --straddle --i-understand-this-trades`, "
                       "puis relancez `run`", kind="straddle")
            return
        try:
            emission = self.v.paper_emission()
        except Exception as e:
            log.warning("Émission illisible (%s)", e)
            emission = None   # the ladder waits; a pair opens without its cost check (judged afterwards)
        plan, note = choose_plan(bal, *mins, emission, self.p)
        if note:
            self._say_once(note)
        if isinstance(plan, Skip):
            self._say_once(f"Pas de cycle: {plan.reason}")
            return
        self._open_pair(now, plan, bal)

    def _open_pair(self, now: float, plan, bal: dict) -> None:
        coin = self.p.coin
        mid = self.v.mid(coin)
        queued = sum(self.v.queued(w) for w in WALLETS)
        paper = sum(self.v.paper_raw(w) for w in WALLETS)
        equity = sum(bal.values()) + queued
        self.st.setdefault("equity_first", equity)
        gap = self.st.get("equity_after_last", equity) - equity
        if gap > 0.5:
            self.st["totals"]["usd_offcycle"] += gap
            log.info("Hors cycle depuis le dernier bilan: -%.2f$ (frais de transfert, positions fermées hors cycle)", gap)
        self.st["equity_after_last"] = equity
        cycle = {
            "n": self.st["totals"]["cycles"] + 1, "started": now, "coin": coin,
            "deadline": now + self.t.open_deadline_s + self.t.submit_margin_s,
            "margin": plan.margin, "leverage": plan.leverage, "close_move": plan.close_move,
            "mid_at_open": mid, "mid_hi": mid, "mid_lo": mid, "aborted": False, "failure_counted": False,
            "free_start": sum(bal.values()), "equity_start": equity, "paper_start": paper,
            "legs": {leg: {"status": "pending", "intent": None, "pos_id": None, "entry": None, "miss": 0,
                           "gone_reads": 0, "close_sends": 0} for leg in LEGS},
            "next_poll": 0, "close_sent_at": None, "settle_at": None,
            "mode": "ladder" if plan.reset else "pair", "reset": plan.reset, "rearms": 0, "rearm": None,
            "min_margin": self.v.minimums()[0],
            "wallets": {leg: "A" if self.p.one_wallet else leg for leg in LEGS},
        }
        where = "sur A" if self.p.one_wallet else "sur A et B"
        if plan.reset:
            log.info("Cycle #%d (échelle): long + short %s %s, marge %.2f$ x%d (notionnel %s$), jambe perdante "
                     "réarmée tous les %.2f%%, gagnant encaissé à +%.2f%% (liquidation à ±%.2f%%), %s=%.2f",
                     cycle["n"], coin, where, plan.margin, plan.leverage, f"{plan.notional:,.0f}",
                     plan.close_move * 100, plan.reset * 100, liq_distance(plan.leverage) * 100, coin, mid)
        else:
            log.info("Cycle #%d: long + short %s %s, marge %.2f$ x%d (notionnel %s$), "
                     "fermeture à ±%.2f%% (liquidation à ±%.2f%%), %s=%.2f",
                     cycle["n"], coin, where, plan.margin, plan.leverage, f"{plan.notional:,.0f}",
                     plan.close_move * 100, liq_distance(plan.leverage) * 100, coin, mid)
        self.st["cycle"] = cycle
        self.st["phase"] = "opening"
        if cycle["mode"] == "ladder" or self.p.one_wallet:
            self.st["schema"] = SCHEMA   # the previous version would misread this cycle: it now refuses the file
        # One leg at a time: PaperTrade can keep an open in its queue for minutes while open interest is
        # full. The side that last waited (or the other side after an open-interest refusal) goes first,
        # alone, so nothing is exposed while it waits; the other follows once it is filled.
        order = ("B", "A") if self.st.get("open_first") == "B" else LEGS
        cycle["second"] = order[1]
        cycle["legs"][order[1]]["status"] = "not_sent"
        self.st["intents_valid_until"] = cycle["deadline"] + self.t.indexer_slack_s
        self.store.save()  # write-ahead: a crash during the request below resumes in 'opening'
        self._send_open(cycle, order[0])
        if cycle["legs"][order[0]]["status"] == "rejected":
            self._open_failed(cycle, "ouverture refusée")

    def _send_open(self, c: dict, name: str) -> None:
        leg = c["legs"][name]
        try:
            leg["intent"] = self.v.open(self._wallet(name), c["coin"], IS_LONG[name], c["margin"], c["leverage"],
                                        self.t.open_deadline_s)
            leg["status"] = "sent"
        except IntentRejected as e:
            leg["status"] = "rejected"
            log.error("Ouverture refusée sur %s: %s", name, e)
            if "openInterest" in str(e):   # the other side lowers the net open interest: start with it
                self.st["open_first"] = "B" if name == "A" else "A"
        except Exception as e:
            leg["status"] = "unknown"
            log.warning("Ouverture sur %s: pas de réponse claire (%s). L'ordre a peut-être été exécuté, je surveille",
                        name, e)
        # Every order was signed before now, so none can execute after now + its lifetime.
        now = self.v.now()
        if leg["status"] in MAYBE_EXECUTED:
            c["deadline"] = now + self.t.open_deadline_s
            self.st["intents_valid_until"] = max(self.st["intents_valid_until"], c["deadline"] + self.t.indexer_slack_s)
        elif not any(c["legs"][n]["status"] in MAYBE_EXECUTED and c["legs"][n]["pos_id"] is None for n in LEGS):
            self.st["intents_valid_until"] = min(self.st["intents_valid_until"], now)

    def _open_failed(self, c: dict, what: str) -> None:
        if c.get("failure_counted"):
            return
        c["failure_counted"] = True
        self.st["open_failures"] += 1
        if self.st["open_failures"] >= 3:
            self.pause(f"3 paires ratées d'affilée (dernière: {what}). Levier, taille, signature ou session key "
                       "refusés ? Vérifiez les logs et le site")

    # --- opening: wait until both legs exist -------------------------------
    def _adverse(self, w: str, mid: float) -> float:
        entry = self.st["cycle"]["legs"][w]["entry"]
        return 1 - mid / entry if IS_LONG[w] else mid / entry - 1

    def _sync_legs(self, leg: str, listed: dict, mid: float, now: float) -> None:
        """Adopts a new position of the leg's wallet as the leg if its order may have executed.
        The callers then sweep each wallet: whatever is not a leg gets closed."""
        c = self.st["cycle"]
        d = c["legs"][leg]
        w = self._wallet(leg)
        taken = {c["legs"][other]["pos_id"] for other in LEGS if other != leg}
        if d["pos_id"] is None and d["status"] in MAYBE_EXECUTED and now < self.st["intents_valid_until"]:
            for p in listed.values():
                if (p.id in self._known(w) or p.id in taken or p.coin not in (None, c["coin"])
                        or p.is_long not in (None, IS_LONG[leg])):
                    continue
                # Not this cycle's order (e.g. a late fill of an earlier one): swept below. Values far
                # off (a unit the parser misreads) are ignored rather than closing the bot's own leg.
                ratio = p.margin / d.get("margin", c["margin"]) if p.margin else 1.0
                wrong_margin = 0.5 <= ratio <= 2 and abs(ratio - 1) > 0.02
                wrong_leverage = p.leverage is not None and 1 <= p.leverage <= 1000 and p.leverage != c["leverage"]
                if wrong_margin or wrong_leverage:
                    continue
                self._remember(w, p.id)
                d["pos_id"] = p.id
                # Unknown entry: assume the worst price seen since the order was sent.
                worst = c["mid_hi"] if IS_LONG[leg] else c["mid_lo"]
                d["entry"] = plausible_price(p.entry, mid) or worst
                log.info("Wallet %s: position #%d ouverte (%s, entrée %.2f)", w, p.id,
                         "long" if IS_LONG[leg] else "short", d["entry"])
                if self.st["phase"] == "closing":   # late fill after an abort: close it with the cycle
                    log.warning("Wallet %s: exécution tardive, je la ferme", w)
                    self._send_close(now, [leg], reset_timer=False)
                break

    def _read_cycle_wallets(self) -> dict[str, dict]:
        """Positions of every wallet of the cycle; a wallet whose read fails is left out."""
        listed = {}
        for w in self._cycle_wallets():
            try:
                listed[w] = self._read(w)
            except Exception as e:
                log.warning("Positions de %s illisibles (%s), nouvel essai", w, e)
        return listed

    def _sync_and_sweep(self, listed: dict, mid: float, now: float) -> None:
        for leg in LEGS:
            if self._wallet(leg) in listed:
                self._sync_legs(leg, listed[self._wallet(leg)], mid, now)
        for w, positions in listed.items():
            self._sweep(w, positions, self._keep(w), now)

    def _opening(self, now: float) -> None:
        c = self.st["cycle"]
        if now < c["next_poll"]:
            return
        c["next_poll"] = now + self.t.fast_poll_s
        mid = self.v.mid(c["coin"])
        c["mid_hi"], c["mid_lo"] = max(c["mid_hi"], mid), min(c["mid_lo"], mid)
        found = [leg for leg in LEGS if c["legs"][leg]["pos_id"] is not None]
        for leg in found:  # a lone leg is naked: never let it run past its exit (needs only the price)
            if len(found) < len(LEGS) and self._adverse(leg, mid) >= c["close_move"]:
                log.warning("Jambe %s seule et déjà à %.2f%% de perte: je la ferme", leg, self._adverse(leg, mid) * 100)
                self._abort(now, found)
                return
        self._sync_and_sweep(self._read_cycle_wallets(), mid, now)
        found = [leg for leg in LEGS if c["legs"][leg]["pos_id"] is not None]
        second = c.get("second")
        if second and c["legs"][second]["status"] == "not_sent" and found and second not in found and not c["aborted"]:
            log.info("Jambe %s exécutée: j'ouvre la jambe %s", found[0], second)
            c["legs"][second]["status"] = "pending"
            self.st["intents_valid_until"] = max(self.st["intents_valid_until"], now + self.t.open_deadline_s
                                                 + self.t.submit_margin_s + self.t.indexer_slack_s)
            self.store.save()   # write-ahead: a crash during the request watches for this order, never resends it
            self._send_open(c, second)
            if c["legs"][second]["status"] == "rejected":
                self._abort(now, found)
                self._open_failed(c, "ouverture refusée")
            return
        if len(found) == len(LEGS):
            self.st["open_failures"] = 0
            self.st["unfilled_opens"] = 0
            c["next_poll"] = now + self.t.positions_every_s
            c["heartbeat"] = now
            self.st["phase"] = "open"
            ea, eb = c["legs"]["A"]["entry"], c["legs"]["B"]["entry"]
            if c.get("mode") == "ladder":
                log.info("Paire ouverte (long %.2f, short %.2f): une jambe est réarmée dès %.2f%% de perte, la "
                         "période se termine quand l'autre gagne %.2f%%", ea, eb, c["close_move"] * 100, c["reset"] * 100)
            else:
                log.info("Paire ouverte (A %.2f, B %.2f): fermeture sous %.2f ou au-dessus de %.2f",
                         ea, eb, ea * (1 - c["close_move"]), eb * (1 + c["close_move"]))
            return
        waiting = [leg for leg in LEGS if c["legs"][leg]["pos_id"] is None and c["legs"][leg]["status"] in MAYBE_EXECUTED]
        if waiting and now < self.st["intents_valid_until"]:
            return
        if found:
            log.warning("Une seule jambe ouverte (%s): je la ferme pour rester neutre", found)
            self._abort(now, found)
            if waiting:
                # It waited in PaperTrade's queue: next time it goes first, alone, until it is filled.
                self.st["open_first"] = waiting[0]
                self._open_failed(c, "une jambe jamais exécutée")
            return
        if not waiting:  # every leg was refused outright (already counted)
            self.st["cycle"] = None
            self.st["phase"] = "idle"
            return
        equity_now = sum(self.v.usdc(w) + self.v.queued(w) for w in WALLETS)   # read before changing the state
        self._cancel_pending(c)
        self.st["cycle"] = None
        self.st["phase"] = "idle"
        if c["equity_start"] - equity_now >= 0.5 * c["margin"]:
            self.pause("les soldes ont baissé mais aucune position n'est visible via l'API: des positions "
                       "sont sans doute ouvertes. Vérifiez sur le site et lancez `python -m ptbot probe`")
            return
        # Nothing left the balances: the orders really did not fill. PaperTrade queues new positions
        # while open interest is full, so try again later; many in a row is abnormal (refused size or
        # leverage, positions the bot cannot see): stop then.
        unfilled = self.st["unfilled_opens"] = self.st.get("unfilled_opens", 0) + 1
        if unfilled >= self.t.unfilled_pause_after:
            self.pause(f"{unfilled} ouvertures d'affilée acceptées mais jamais exécutées avant leur échéance: vérifiez "
                       "sur le site (file d'attente pleine ? positions invisibles ?) puis `resume`")
            return
        self.st["next_try"] = now + self.t.unfilled_retry_s
        log.warning("Ordres acceptés mais pas exécutés avant leur échéance (file d'attente de PaperTrade pleine ?): "
                    "nouvel essai dans %.0f min (%d/%d)", self.t.unfilled_retry_s / 60, unfilled,
                    self.t.unfilled_pause_after)

    def _abort(self, now: float, wallets: list[str]) -> None:
        self.st["cycle"]["aborted"] = True
        self._cancel_pending(self.st["cycle"])
        self._send_close(now, wallets)
        self.st["phase"] = "closing"

    def _cancel_pending(self, c: dict) -> None:
        """Best effort: kill the orders of legs not seen yet. The bot keeps watching for them
        until they expire anyway, in case the cancellation does not go through."""
        cancel = getattr(self.v, "cancel", None)
        for name in LEGS:
            leg = c["legs"][name]
            if not cancel or leg["pos_id"] is not None or leg["status"] not in MAYBE_EXECUTED or not leg["intent"]:
                continue
            if leg.get("cancel_sent") == leg["intent"]:
                continue
            leg["cancel_sent"] = leg["intent"]
            w = self._wallet(name)
            try:
                cancel(w, leg["intent"])
                log.info("Wallet %s: ordre %s annulé", w, leg["intent"])
            except Exception as e:
                log.warning("Wallet %s: annulation de l'ordre %s impossible (%s), je surveille jusqu'à son échéance",
                            w, leg["intent"], e)

    # --- open: watch the price, close (or re-arm) legs before liquidation ----
    def _open(self, now: float) -> None:
        c = self.st["cycle"]
        if now >= c.get("next_key_check", 0):
            c["next_key_check"] = now + self.t.key_check_s
            if not self._keys_ok(KEY_MIN_LEFT_S):
                self._end_period(now, "Session key bientôt expirée (le cycle a duré trop longtemps)")
                return
        mid = self.v.mid(c["coin"])
        worst = max(self._adverse(leg, mid) for leg in LEGS)
        if c.get("mode") == "ladder":
            if self._ladder_check(now, mid):
                return
        elif worst >= c["close_move"] - 1e-12:
            log.info("%s=%.2f: une jambe perd %.3f%%, fermeture des deux jambes", c["coin"], mid, worst * 100)
            self._send_close(now, list(LEGS))
            self.st["phase"] = "closing"
            return
        if now >= c["next_poll"]:
            # One leg per poll, so a slow read never delays the price check above for long.
            leg = LEGS[c.get("read_turn", 0) % len(LEGS)]
            w = self._wallet(leg)
            c["next_poll"] = now + self.t.positions_every_s / len(LEGS)
            try:
                listed = self._read(w)
            except Exception as e:
                log.warning("Positions de %s illisibles (%s): je continue de surveiller le prix", w, e)
                return
            self._sweep(w, listed, self._keep(w), now)
            d = c["legs"][leg]
            if d["pos_id"] in listed:
                d["miss"] = 0
                c["read_turn"] = c.get("read_turn", 0) + 1
            else:
                d["miss"] += 1
                if d["miss"] >= self.t.missing_reads or self._adverse(leg, mid) >= liq_distance(c["leverage"]):
                    log.warning("Jambe %s disparue (liquidée ou fermée à la main): je ferme la paire", leg)
                    self._send_close(now, list(LEGS))  # both: if the "missing" one still exists, close it too
                    self.st["phase"] = "closing"
                    return
                c["next_poll"] = now + self.t.fast_poll_s  # one miss may be a glitch: re-check this leg quickly
        if now - c.get("heartbeat", 0) >= self.t.heartbeat_s:
            c["heartbeat"] = now
            if c.get("mode") == "ladder":
                log.info("Échelle: %s=%.2f, long %+.3f%%, short %+.3f%% (réarmement à -%.2f%%, encaissement à "
                         "+%.2f%%), %d réarmement(s)", c["coin"], mid, -self._adverse("A", mid) * 100,
                         -self._adverse("B", mid) * 100, c["close_move"] * 100, c["reset"] * 100, c.get("rearms", 0))
            else:
                log.info("Paire ouverte: %s=%.2f, jambe la plus en perte %.3f%% (fermeture à %.2f%%)",
                         c["coin"], mid, worst * 100, c["close_move"] * 100)

    # --- ladder: re-arm the losing leg, realise the winner on a large move ----
    def _ladder_check(self, now: float, mid: float) -> bool:
        """Ends the period or starts re-arming a leg; True when the phase changed."""
        c = self.st["cycle"]
        gains = {leg: -self._adverse(leg, mid) for leg in LEGS}
        winner = max(LEGS, key=lambda leg: gains[leg])
        if gains[winner] >= c["reset"] - 1e-12:
            log.info("%s=%.2f: jambe %s à +%.3f%%, j'encaisse (fermeture des deux jambes, %d réarmement(s))",
                     c["coin"], mid, winner, gains[winner] * 100, c.get("rearms", 0))
            self._send_close(now, list(LEGS))
            self.st["phase"] = "closing"
            return True
        loser = min(LEGS, key=lambda leg: gains[leg])
        if -gains[loser] >= c["close_move"] - 1e-12:
            if c.get("rearm_refused"):
                # Its last re-open was refused: the leg stays open (the pair is still hedged) until the
                # retry, unless it gets halfway to its liquidation.
                if -gains[loser] >= (c["close_move"] + liq_distance(c["leverage"])) / 2:
                    self._end_period(now, f"jambe {loser} à {gains[loser]:.3%} et réouverture refusée")
                    return True
                if now < c.get("rearm_retry_at", 0):
                    return False
            self._start_rearm(now, loser, mid)
            return self.st["phase"] != "open"   # a refused re-open leaves the pair as it was
        return False

    def _start_rearm(self, now: float, leg: str, mid: float) -> None:
        """Re-opens the losing leg and closes the old one only once the new position exists: an
        accepted order can wait in PaperTrade's open-interest queue for minutes, and the old leg
        keeps the pair hedged meanwhile. A refused re-open leaves the pair as it was, to be retried
        at the next checks."""
        c = self.st["cycle"]
        d = c["legs"][leg]
        w = self._wallet(leg)
        try:
            free = self.v.usdc(w)
        except Exception as e:
            # The losing leg must not wait for the balance: an open too big for the wallet is refused,
            # and refused re-opens end the period.
            log.warning("Solde de %s illisible (%s): je réarme quand même", w, e)
            free = None
        margin = self._rearm_margin(leg, mid)
        if free is not None and free < margin + 0.05:   # the new leg locks its margin before the old one gives its back
            self._end_period(now, f"Wallet {w}: {free:.2f}$ libres, trop peu pour réarmer la jambe {leg}")
            return
        notional = d.get("margin", c["margin"]) * c["leverage"]
        loss = self._adverse(leg, mid) * notional
        c["rearm"] = {"leg": leg, "old_id": d["pos_id"], "old_entry": d["entry"], "old_leg": dict(d), "mid": mid,
                      "started": now, "gone_reads": 0, "close_sends": 0, "close_sent_at": None, "attempts": 0,
                      "retry_at": 0.0}
        d.update(status="not_sent", intent=None, pos_id=None, entry=None, miss=0, gone_reads=0, close_sends=0)
        d.pop("first_close_at", None)
        self.st["phase"] = "rearming"
        c["next_poll"] = now + self.t.fast_poll_s
        log.info("%s=%.2f: jambe %s à -%.3f%%, je la rouvre et ferme l'ancienne (~%.2f$ de perte, PAPER mintés)",
                 c["coin"], mid, leg, loss / notional * 100, loss)
        self._rearm_open(now, mid)   # saves the state before its request: a crash resumes in 'rearming'
        if d["status"] == "rejected":
            self._undo_rearm(now)
            return
        c.pop("rearm_refused", None)   # the old leg is closed by _rearming once the new one is seen

    def _undo_rearm(self, now: float) -> None:
        """The re-open was refused before anything changed: back to the pair as it was."""
        c = self.st["cycle"]
        r = c["rearm"]
        d = c["legs"][r["leg"]]
        d.clear()
        d.update(r["old_leg"])
        c["rearm"] = None
        c["rearm_refused"] = True
        c["rearm_retry_at"] = self.v.now() + self.t.rearm_retry_s   # after the request, however long it took
        self.st["intents_valid_until"] = now
        self.st["phase"] = "open"
        c["next_poll"] = now + self.t.fast_poll_s
        log.info("Jambe %s gardée (couverte) jusqu'au prochain essai", r["leg"])

    def _rearm_margin(self, leg: str, mid: float) -> float:
        """Margin of a re-opened leg: the same BTC quantity as the other leg (same dollar notional
        at another price would leave the pair slightly long or short, a cost that depends on the
        trend's direction), never under the protocol's minimum margin."""
        c = self.st["cycle"]
        other = c["legs"]["B" if leg == "A" else "A"]
        margin = other.get("margin", c["margin"]) * mid / other["entry"] if other.get("entry") else c["margin"]
        return max(floor_cents(margin), c.get("min_margin", 0.0))

    def _rearm_close(self, now: float) -> None:
        c = self.st["cycle"]
        r = c["rearm"]
        r["close_sends"] += 1
        r["close_sent_at"] = now
        r.setdefault("first_close_at", now)
        self.store.save()  # write-ahead: after a crash the other leg is known to be possibly alone
        try:
            self.v.close(self._wallet(r["leg"]), [r["old_id"]])
        except Exception as e:
            log.error("Fermeture #%s (jambe %s) refusée: %s (nouvel essai)", r["old_id"], r["leg"], e)

    def _rearm_open(self, now: float, mid: float) -> None:
        c = self.st["cycle"]
        r = c["rearm"]
        leg = r["leg"]
        d = c["legs"][leg]
        r["attempts"] += 1
        r["retry_at"] = now + self.t.rearm_retry_s
        c["mid_hi"] = c["mid_lo"] = mid
        d.update(status="pending", intent=None, margin=self._rearm_margin(leg, mid))
        self.st["intents_valid_until"] = now + self.t.open_deadline_s + self.t.submit_margin_s + self.t.indexer_slack_s
        self.store.save()  # write-ahead: a crash during the request watches for this order
        try:
            d["intent"] = self.v.open(self._wallet(leg), c["coin"], IS_LONG[leg], d["margin"], c["leverage"],
                                      self.t.open_deadline_s)
            d["status"] = "sent"
        except IntentRejected as e:
            d["status"] = "rejected"
            log.error("Réouverture de la jambe %s refusée: %s", leg, e)
        except Exception as e:
            d["status"] = "unknown"
            log.warning("Réouverture de la jambe %s: pas de réponse claire (%s), je surveille", leg, e)
        live = d["status"] in MAYBE_EXECUTED
        self.st["intents_valid_until"] = self.v.now() + self.t.open_deadline_s + self.t.indexer_slack_s if live else now

    def _end_period(self, now: float, why: str, cut: bool = True) -> None:
        """Ends a cycle early: closes what is open (a re-armed leg's old position is swept as a
        stray) and books it once everything is gone, outside the cost cap's window (a cut cycle
        realises its winner on a small move, an expensive one that says nothing about the farm)."""
        c = self.st["cycle"]
        log.warning("%s: fin de %s", why, "la période" if c.get("mode") == "ladder" else "la paire")
        if cut:
            c["cut"] = why
        self._cancel_pending(c)
        c["rearm"] = None
        self._send_close(now, list(LEGS))
        self.st["phase"] = "closing"

    def _rearming(self, now: float) -> None:
        c = self.st["cycle"]
        r = c["rearm"]
        if now < c["next_poll"]:
            return
        c["next_poll"] = now + self.t.fast_poll_s
        mid = self.v.mid(c["coin"])
        c["mid_hi"], c["mid_lo"] = max(c["mid_hi"], mid), min(c["mid_lo"], mid)
        r.setdefault("mid", mid)   # a re-arm saved by an earlier version
        leg = r["leg"]
        other = "B" if leg == "A" else "A"
        d = c["legs"][leg]
        w = self._wallet(leg)
        move = -self._adverse(other, mid)
        if move >= c["reset"] - 1e-12:   # the period's normal end, reached during a re-arm
            self._end_period(now, f"jambe {other} à {move:+.3%} pendant le réarmement de {leg}, j'encaisse", cut=False)
            return
        if -move >= c["close_move"] - 1e-12:
            self._end_period(now, f"jambe {other} à {move:+.3%} pendant le réarmement de {leg}")
            return
        try:
            listed = self._read(w)
        except Exception as e:
            log.warning("Positions de %s illisibles (%s)", w, e)
            listed = None
            if r["old_id"] is not None and r["close_sends"] and now - r["close_sent_at"] >= self.t.close_retry_s:
                self._rearm_close(now)   # closes are idempotent: re-send blindly
        if listed is not None and d["pos_id"] is None:
            self._sync_legs(leg, listed, mid, now)
        if r["old_id"] is not None and not r["close_sends"]:
            # The new order is accepted but not seen yet (queued, or a restart): the old leg still
            # hedges the pair, it is closed once the new one exists.
            waiting = d["status"] in MAYBE_EXECUTED and now < self.st["intents_valid_until"]
            if d["pos_id"] is not None:
                self._rearm_close(now)
            elif listed is not None and not waiting:
                if r.get("old_leg"):
                    log.warning("Jambe %s: la réouverture n'a jamais été exécutée, je garde l'ancienne", leg)
                    self._undo_rearm(now)
                    return
                self._rearm_close(now)
            elif now - r.get("queued_log", 0) >= 60:
                r["queued_log"] = now
                log.info("Jambe %s: réouverture acceptée mais pas encore exécutée (file d'attente ?), l'ancienne "
                         "reste ouverte en attendant", leg)
        # From the old leg's close until the new one shows up, the other leg may be alone: bound that
        # time, and the gain it can give back meanwhile.
        if d["pos_id"] is None and r["close_sends"]:
            since = (1 - mid / r["mid"]) if IS_LONG[other] else (mid / r["mid"] - 1)
            if since >= c["close_move"] - 1e-12:
                self._end_period(now, f"jambe {other} seule et {since:.3%} contre elle depuis le réarmement de {leg}")
                return
            alone = now - r.get("first_close_at", r["started"])
            if alone >= max(self.t.rearm_naked_s, self.t.open_deadline_s / 2):
                self._end_period(now, f"jambe {leg} toujours pas rouverte {alone:.0f} s après la fermeture de l'ancienne")
                return
        elif d["pos_id"] is not None and self._adverse(leg, mid) >= c["close_move"] - 1e-12:
            self._end_period(now, f"jambe {leg} rouverte déjà à {-self._adverse(leg, mid):.3%}")
            return
        if listed is None:
            return
        if r["old_id"] is not None:
            if r["old_id"] in listed:
                r["gone_reads"] = 0
                if r["close_sends"] >= self.t.max_close_sends:
                    # The closing phase knows how to deal with a position that will not go away.
                    self._end_period(now, f"position #{r['old_id']} toujours listée après {r['close_sends']} fermetures")
                    return
                old_adverse = (1 - mid / r["old_entry"]) if IS_LONG[leg] else (mid / r["old_entry"] - 1)
                urgent = old_adverse >= (c["close_move"] + liq_distance(c["leverage"])) / 2
                if urgent and not r["close_sends"] and d["pos_id"] is None:
                    # Halfway to its liquidation and still no replacement: close everything, and cancel
                    # the waiting order (a late fill is closed with the period).
                    self._end_period(now, f"jambe {leg} à {-old_adverse:.3%} et réouverture toujours en attente")
                    return
                if urgent or (r["close_sends"] and now - r["close_sent_at"] >= self.t.close_retry_s):
                    self._rearm_close(now)
            else:
                r["gone_reads"] += 1
                if r["gone_reads"] >= self.t.missing_reads:
                    r["old_id"] = None
        if d["pos_id"] is None:
            expired = d["status"] in MAYBE_EXECUTED and now >= self.st["intents_valid_until"]
            if d["status"] in ("not_sent", "rejected") or expired:
                if expired:
                    log.warning("Jambe %s: la réouverture n'a jamais été exécutée", leg)
                    d["status"] = "not_sent"
                if r["attempts"] >= self.t.rearm_attempts:
                    self._end_period(now, f"jambe {leg} impossible à rouvrir après {r['attempts']} essais")
                    return
                if now >= r["retry_at"]:
                    self._rearm_open(now, mid)
        self._sweep(w, listed, self._keep(w), now)
        if r["old_id"] is None and d["pos_id"] is not None:
            c["rearm"] = None
            c["rearms"] = c.get("rearms", 0) + 1
            self.st["phase"] = "open"
            c["next_poll"] = now + self.t.positions_every_s / len(LEGS)
            log.info("Jambe %s réarmée (entrée %.2f), réarmement n°%d de la période", leg, d["entry"], c["rearms"])

    def _send_close(self, now: float, legs: list[str], reset_timer: bool = True) -> None:
        """Legs sharing a wallet go in one Close intent, so they fill at the same price."""
        c = self.st["cycle"]
        batches: dict[str, list] = {}
        for name in legs:
            leg = c["legs"][name]
            if leg["pos_id"] is None:
                continue
            leg["close_sends"] += 1
            leg.setdefault("first_close_at", now)
            batches.setdefault(self._wallet(name), []).append((name, leg["pos_id"]))
        for w, batch in batches.items():
            try:
                self.v.close(w, [pid for _, pid in batch])
            except IntentRejected as e:
                if len(batch) == 1:
                    log.error("Fermeture #%s (jambe %s) refusée: %s (nouvel essai)", batch[0][1], batch[0][0], e)
                    continue
                log.warning("Fermeture groupée sur %s refusée (%s): une par une", w, e)
                for name, pid in batch:
                    try:
                        self.v.close(w, [pid])
                    except Exception as e2:
                        log.error("Fermeture #%s (jambe %s) refusée: %s (nouvel essai)", pid, name, e2)
            except Exception as e:
                log.error("Fermeture %s (jambes %s) sans réponse claire: %s (nouvel essai)",
                          [pid for _, pid in batch], [name for name, _ in batch], e)
        if reset_timer or c["close_sent_at"] is None:
            c["close_sent_at"] = now
        c["next_poll"] = now + self.t.fast_poll_s

    # --- closing: wait for both legs to be gone, then book the cycle --------
    def _closing(self, now: float) -> None:
        c = self.st["cycle"]
        if now < c["next_poll"]:
            return
        c["next_poll"] = now + self.t.fast_poll_s
        mid = self.v.mid(c["coin"])
        try:
            listed = {w: self._read(w) for w in self._cycle_wallets()}
        except Exception as e:
            # Closes are idempotent: keep re-sending blindly the ones not yet confirmed gone.
            log.warning("Positions illisibles (%s): je renvoie les fermetures à l'aveugle si besoin", e)
            pending = [leg for leg in LEGS if c["legs"][leg]["pos_id"] is not None
                       and c["legs"][leg]["gone_reads"] < self.t.missing_reads]
            if pending and self._should_resend(c, pending, mid, now):
                self._send_close(now, pending)
            return
        self._sync_and_sweep(listed, mid, now)
        visible, unconfirmed = [], []
        for name in LEGS:
            leg = c["legs"][name]
            if leg["pos_id"] is None:
                continue
            if leg["pos_id"] in listed[self._wallet(name)]:
                leg["gone_reads"] = 0
                visible.append(name)
            else:
                leg["gone_reads"] += 1
                if leg["gone_reads"] < self.t.missing_reads:
                    unconfirmed.append(name)
        waiting = [leg for leg in LEGS if c["legs"][leg]["pos_id"] is None
                   and c["legs"][leg]["status"] in MAYBE_EXECUTED and now < self.st["intents_valid_until"]]
        if visible:
            sends = max(c["legs"][w]["close_sends"] for w in visible)
            if self._should_resend(c, visible, mid, now):
                log.warning("Toujours ouvert sur %s: je renvoie la fermeture", visible)
                self._send_close(now, visible)
            first = min(c["legs"][w].get("first_close_at", now) for w in visible)
            if sends >= self.t.max_close_sends and now - first >= self.t.ghost_after_s and not c.get("ghost_paused"):
                c["ghost_paused"] = True
                self.pause(f"positions {[c['legs'][w]['pos_id'] for w in visible]} toujours listées après "
                           f"{sends} fermetures. Si le site les montre fermées, l'API est mal lue; sinon fermez-les "
                           "à la main", kind="ghost")
        if visible or unconfirmed or waiting or self.st["strays"]:
            c["settle_at"] = None
            return
        if c["settle_at"] is None:
            c["settle_at"] = now + self.t.settle_s
            return
        if now >= c["settle_at"]:
            self._book_cycle(now)

    def _should_resend(self, c: dict, wallets: list[str], mid: float, now: float) -> bool:
        """Re-send a close every close_retry_s (every ghost_retry_s after max_close_sends), and at
        once when a leg is halfway from its exit to liquidation, unless it already looks like a ghost."""
        sends = max(c["legs"][w]["close_sends"] for w in wallets)
        urgent = any(self._adverse(w, mid) >= (c["close_move"] + liq_distance(c["leverage"])) / 2 for w in wallets)
        interval = self.t.close_retry_s if sends < self.t.max_close_sends else self.t.ghost_retry_s
        return (urgent and sends < self.t.max_close_sends) or now - c["close_sent_at"] >= interval

    def _book_cycle(self, now: float) -> None:
        c = self.st["cycle"]
        free = sum(self.v.usdc(w) for w in WALLETS)
        queued = sum(self.v.queued(w) for w in WALLETS)
        paper = (sum(self.v.paper_raw(w) for w in WALLETS) - c["paper_start"]) / SCALE
        legs_opened = sum(1 for leg in c["legs"].values() if leg["pos_id"] is not None)
        returned = free + queued - (c["equity_start"] - legs_opened * c["margin"])
        self.st["cycle"] = None
        self.st["phase"] = "idle"
        self.st["next_try"] = 0
        self._clear_pause("ghost")
        if legs_opened and returned < 0.25 * c["margin"] and paper <= 0:
            # Neither margin came back nor PAPER was minted: the legs may just be invisible.
            self.pause("les positions ne sont plus visibles via l'API mais ni marge ni PAPER ne sont revenus: "
                       "elles sont peut-être encore ouvertes. Vérifiez sur le site avant `resume`")
            return
        usd = c["equity_start"] - free - queued
        self.st["equity_after_last"] = free + queued
        tot = self.st["totals"]
        tot["cycles"] += 1
        tot["usd_spent"] += usd
        ea, eb = c["legs"]["A"]["entry"], c["legs"]["B"]["entry"]
        # Pair legs filled far apart: not a normal cycle (ladder legs are re-opened at other prices on purpose).
        skewed = c.get("mode") != "ladder" and bool(ea and eb and abs(ea / eb - 1) > 0.002)
        kind = "aborted" if c["aborted"] else "cut" if c.get("cut") else "skewed" if skewed else "normal"
        if paper < 0:
            # Some PAPER left the wallet during the cycle (a stake made by hand): the cycle's mint is unknown.
            log.warning("Cycle #%d: PAPER du cycle inconnu (stake manuel pendant le cycle ?), exclu du plafond", c["n"])
            kind, paper = "unknown", 0.0
        tot["paper_minted"] += paper
        mode, rearms = c.get("mode", "pair"), c.get("rearms", 0)
        if kind != "unknown":
            self.st["cycles_all"].append({"ts": now, "usd": usd, "paper": paper, "kind": kind, "mode": mode,
                                          "rearms": rearms})
            del self.st["cycles_all"][:-1000]
        if kind == "normal":
            self.st["history"].append({"ts": now, "usd": usd, "paper": paper, "mode": mode, "rearms": rearms})
            del self.st["history"][:-1000]
        note = {"aborted": "(avorté) ", "cut": "(écourté, hors plafond par cycle) ", "skewed": "(entrées décalées) ",
                "normal": "", "unknown": "(PAPER inconnu) "}[kind]
        if mode == "ladder":
            note += f"(échelle, {rearms} réarmement(s)) "
        log.info("Cycle #%d %sterminé en %.0f min: coût %.2f$, +%s PAPER (%.2f$/1000). Total: %s PAPER pour %.2f$",
                 c["n"], note, (now - c["started"]) / 60, usd, f"{paper:,.0f}",
                 cost_per_1000(usd, paper), f"{tot['paper_minted']:,.0f}", tot["usd_spent"])
        hit, avg = cost_cap_hit(self.st["history"], self.p)
        everything = self.st["cycles_all"]
        if mode != "ladder":
            everything = [x for x in everything if x.get("mode") != "ladder"]
        overall = cost_per_1000(sum(x["usd"] for x in everything), sum(x["paper"] for x in everything))
        if hit:
            if mode == "ladder":
                span = "les dernières périodes (nettement au-delà du bruit)"
            else:
                span = "le dernier cycle" if self.p.cost_window <= 1 else f"les {self.p.cost_window} derniers cycles"
            self.pause(f"coût {avg:.2f}$/1000 PAPER sur {span} > plafond {self.p.max_cost_per_1000:.2f}$: "
                       "farming arrêté")
        elif mode == "ladder":
            hit_all, avg_all = ladder_cost_hit([x for x in everything if x.get("mode") == "ladder"], self.p)
            if hit_all:
                self.pause(f"coût {avg_all:.2f}$/1000 PAPER sur les dernières périodes (écourtées comprises) > "
                           f"plafond {self.p.max_cost_per_1000:.2f}$: farming arrêté")
        elif overall > self.p.max_cost_per_1000 and (
                len(everything) >= 3
                or sum(x["usd"] for x in everything) >= 0.25 * self.st.get("equity_first", float("inf"))):
            self.pause(f"coût cumulé {overall:.2f}$/1000 PAPER (cycles avortés compris) > plafond "
                       f"{self.p.max_cost_per_1000:.2f}$: farming arrêté")

    # --- staking --------------------------------------------------------
    def _track_stakes(self, now: float) -> None:
        pending = self.st["pending_stakes"]
        if not pending or now < self.st.get("next_stake_track", 0):
            return
        self.st["next_stake_track"] = now + self.t.stake_track_s
        for w in list(pending):
            ps = pending[w]
            # Dropped by at least half the stake: executed, even if some PAPER was minted meanwhile.
            if self.v.paper_raw(w) <= ps["before"] - ps["raw"] // 2:
                self.st["totals"]["paper_staked"] += ps["raw"] / SCALE
                log.info("Wallet %s: stake de %s PAPER confirmé", w, f"{ps['raw'] / SCALE:,.0f}")
                del pending[w]
                self.st["stake_failures"].pop(w, None)
            elif now > ps["until"]:
                log.warning("Wallet %s: stake jamais exécuté (ordre expiré), il sera renvoyé", w)
                del pending[w]
                self._stake_failed(w, now)

    def _stake_failed(self, w: str, now: float) -> None:
        n = self.st["stake_failures"][w] = self.st["stake_failures"].get(w, 0) + 1
        if n >= 3:
            self.st["stake_failures"][w] = 0
            self.st["stake_disabled_until"][w] = now + self.t.stake_disable_s
            log.error("Wallet %s: 3 stakes en échec, staking automatique suspendu %.0f h pour ce wallet. Le "
                      "farming continue. Pour staker à la main, arrêtez d'abord le bot (fichier STOP) puis "
                      "`python -m ptbot stake-now`", w, self.t.stake_disable_s / 3600)

    def stake_if_needed(self, now: float, force: bool = False) -> None:
        """Stakes each wallet's whole unstaked PAPER once it reaches the threshold.
        Called between cycles (PAPER is minted when positions close); no pair is
        opened while a stake is unconfirmed, so it cannot skew a cycle's count."""
        if not force and now - self._last_stake_check < self.t.stake_every_s:
            return
        self._last_stake_check = now
        threshold = int(self.p.stake_threshold * SCALE)
        for w in WALLETS:
            if w in self.st["pending_stakes"] or now < self.st["stake_disabled_until"].get(w, 0):
                continue
            raw = self.v.paper_raw(w)
            if raw <= 0 or raw < threshold:
                continue
            self.st["pending_stakes"][w] = {"raw": raw, "before": raw,
                                            "until": now + STAKE_DEADLINE_S + self.t.indexer_slack_s}
            self.store.save()
            try:
                self.v.stake(w, raw)
            except IntentRejected as e:
                del self.st["pending_stakes"][w]
                log.error("Stake refusé sur %s: %s", w, e)
                self._stake_failed(w, now)
                continue
            except Exception as e:
                log.warning("Stake sur %s: pas de réponse claire (%s), je vérifie on-chain", w, e)
                continue
            log.info("Wallet %s: stake de %s PAPER envoyé", w, f"{raw / SCALE:,.0f}")
        self.st["next_stake_track"] = 0
        self._track_stakes(now)

    # --- rebalancing: A <-> B through HyperCore -----------------------------
    def _set_step(self, r: dict, step: str, now: float) -> None:
        r.update(step=step, step_started=now, warned=now)

    def _rebalancing(self, now: float) -> None:
        r = self.st["rebalance"]
        if now < r["next_poll"]:
            return
        r["next_poll"] = now + self.t.rebalance_poll_s
        src, dst, amount = r["src"], r["dst"], r["amount"]
        waited = now - r["step_started"]
        if r["step"] in ("await_core", "await_credit") and waited >= self.t.rebalance_give_up_s:
            what = "l'arrivée des USDC sur HyperCore" if r["step"] == "await_core" else f"le crédit sur {dst}"
            self._finish_rebalance(f"rééquilibrage: toujours en attente de {what} après {waited / 3600:.1f} h, "
                                   "vérifiez les soldes PaperTrade et HyperCore à la main")
            return
        if r["step"] == "withdraw":
            if not self.v.deposit_address(dst):
                self._finish_rebalance(f"adresse de dépôt PaperTrade du wallet {dst} manquante "
                                       f"(DEPOSIT_ADDRESS_{dst} dans .env)")
                return
            if r["withdraw_attempts"] >= 3:
                self._finish_rebalance(f"le retrait de {amount:.2f}$ depuis {src} n'a jamais été exécuté (3 essais)")
                return
            spot, perp = self.v.core_spot_usdc(src), self.v.core_perp_usdc(src)
            src_before = self.v.usdc(src) + self.v.queued(src)
            r.update(spot_before=spot, perp_before=perp, src_before=src_before)
            r["withdraw_attempts"] += 1
            self._set_step(r, "await_core", now)
            self.store.save()  # write-ahead: a restart waits for this withdrawal instead of sending another
            try:
                self.v.withdraw_to_core(src, amount)
            except IntentRejected as e:
                self._finish_rebalance(f"retrait de {amount:.2f}$ depuis {src} refusé: {e}")
                return
            except Exception as e:
                log.warning("Rééquilibrage: retrait sans réponse claire (%s), je surveille HyperCore", e)
                return
            log.info("Rééquilibrage: retrait de %.2f$ de PaperTrade %s vers HyperCore", amount, src)
        elif r["step"] == "await_core":
            received = self.v.core_spot_usdc(src) - r["spot_before"]
            if _arrived(received, amount):
                r["send_amount"] = floor_cents(min(amount, received))
                self._set_step(r, "send", now)
                r["next_poll"] = 0
                return
            in_perp = self.v.core_perp_usdc(src) - r["perp_before"]
            if _arrived(in_perp, amount) and r.get("perp_moves", 0) < 3:
                log.info("Rééquilibrage: USDC arrivés en perp sur HyperCore, passage en spot")
                r["perp_moves"] = r.get("perp_moves", 0) + 1
                self.v.core_perp_to_spot(src, floor_cents(min(amount, in_perp)))
                return
            if (waited > WITHDRAW_DEADLINE_S + self.t.indexer_slack_s
                    and self.v.usdc(src) + self.v.queued(src) >= r["src_before"] - 0.5):
                log.warning("Rééquilibrage: le retrait a expiré sans être exécuté, nouvel essai")
                self._set_step(r, "withdraw", now)
                return
            self._rebalance_wait(r, now, "l'arrivée des USDC sur HyperCore")
        elif r["step"] == "send":
            if r["send_amount"] <= 0:
                self._finish_rebalance(f"rien à envoyer vers {dst}: vérifiez le spot HyperCore de {src}")
                return
            if r["send_amount"] < MIN_DEPOSIT_USD:   # PaperTrade would not credit it
                self._finish_rebalance(f"{r['send_amount']:.2f}$ arrivés sur le spot HyperCore de {src}, sous le "
                                       f"minimum de dépôt de {MIN_DEPOSIT_USD:.0f}$: déposez-les à la main")
                return
            if r["send_attempts"] >= 3:
                self._finish_rebalance(f"envoi HyperCore {src} -> dépôt {dst} en échec 3 fois: "
                                       f"{r['send_amount']:.2f}$ sont sur le spot HyperCore de {src}")
                return
            dest = self.v.deposit_address(dst)
            if "dst_before" not in r:  # first attempt only: a retry must compare with the original balances
                r["dst_before"] = self.v.usdc(dst) + self.v.queued(dst)
                r["spot_before_send"] = self.v.core_spot_usdc(src)
            r["send_attempts"] += 1
            r["unsent_since"] = None
            self._set_step(r, "await_credit", now)
            self.store.save()
            try:
                self.v.core_spot_send(src, dest, r["send_amount"])
            except Exception as e:
                log.warning("Rééquilibrage: envoi sans confirmation (%s), je vérifie", e)
                return
            log.info("Rééquilibrage: %.2f$ envoyés vers l'adresse de dépôt de %s (%s)", r["send_amount"], dst, dest)
        elif r["step"] == "await_credit":
            if _arrived(self.v.usdc(dst) + self.v.queued(dst) - r["dst_before"], r["send_amount"]):
                log.info("Rééquilibrage terminé: A=%.2f$, B=%.2f$", self.v.usdc("A"), self.v.usdc("B"))
                self._finish_rebalance(None)
                return
            if self.v.core_spot_usdc(src) >= r["spot_before_send"] - 0.01:   # nothing has left the spot account
                r["unsent_since"] = r.get("unsent_since") or now
                if now - r["unsent_since"] >= self.t.unsent_after_s:
                    log.warning("Rééquilibrage: l'envoi n'est jamais parti, nouvel essai")
                    self._set_step(r, "send", now)
                    r["next_poll"] = 0
                    return
            else:
                r["unsent_since"] = None
            self._rebalance_wait(r, now, f"le crédit sur le compte PaperTrade de {dst}")

    def _rebalance_wait(self, r: dict, now: float, what: str) -> None:
        """No position is open while rebalancing, so waiting is safe: warn every so often."""
        if now - r["warned"] >= self.t.rebalance_warn_s:
            r["warned"] = now
            log.warning("Rééquilibrage: j'attends %s depuis %.0f min", what, (now - r["step_started"]) / 60)

    def _finish_rebalance(self, error: str | None) -> None:
        self.st["rebalance"] = None
        self.st["phase"] = "idle"
        self.st["next_try"] = 0
        if error:
            self.pause(error)
