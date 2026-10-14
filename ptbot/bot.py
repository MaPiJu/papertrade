"""Delta-neutral state machine: idle -> opening -> open -> closing -> idle,
with a `rebalancing` detour when the two USDC balances drift apart.

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
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path

from .constants import SCALE, STAKE_DEADLINE_S, WITHDRAW_DEADLINE_S
from .papertrade_api import IntentRejected
from .positions import Position, plausible_price
from .strategy import (Params, Skip, cost_cap_hit, cost_per_1000, floor_cents, liq_distance, plan_open,
                       plan_rebalance)

log = logging.getLogger("ptbot")

WALLETS = ("A", "B")
IS_LONG = {"A": True, "B": False}   # A carries the long leg, B the short leg
# Leg statuses whose order may still execute ('pending' = state saved before the request, then a crash).
MAYBE_EXECUTED = ("sent", "unknown", "pending")
SCHEMA = 3


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
    max_close_sends: int = 8         # closes of one position before pausing for a human
    settle_s: float = 5.0
    idle_retry_s: float = 60.0
    stray_retry_s: float = 15.0
    stake_disable_s: float = 6 * 3600.0
    rebalance_poll_s: float = 10.0
    unsent_after_s: float = 90.0
    rebalance_warn_s: float = 900.0
    rebalance_give_up_s: float = 7200.0
    heartbeat_s: float = 60.0


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
    st.update(paused=None, next_try=0, open_failures=0, stake_failures={}, stake_disabled_until={})
    for s in st.get("strays", {}).values():
        s["attempts"] = 0
    if reset_costs:
        st["history"] = []
        st["cycles_all"] = []


def _arrived(delta: float, amount: float) -> bool:
    """A transfer counts as arrived net of small fees (and never when nothing came)."""
    return delta > 0 and delta >= amount - min(1.5, 0.05 * amount)


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
        self._migrate()

    def _migrate(self) -> None:
        if self.st.get("schema") != SCHEMA:
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
    def run(self, max_steps: int | None = None, stop_file: Path | None = None) -> None:
        restore = self._catch_signals()
        steps = errors = 0
        try:
            while (max_steps is None or steps < max_steps) and not self._stop:
                if stop_file and stop_file.exists():
                    log.warning("Fichier %s présent: arrêt du bot", stop_file)
                    return
                try:
                    self.step()
                    errors = 0
                except Exception:
                    errors += 1
                    self.store.save()
                    log.exception("Erreur pendant le cycle (nouvel essai au prochain tour)")
                    self.v.sleep(min(5 * errors, 60))   # back off while an API is down
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
        resume_file = self.store.path and self.store.path.parent / "RESUME"   # written by `python -m ptbot resume`
        if resume_file and resume_file.exists():
            self.resume(reset_costs="reset-costs" in resume_file.read_text())
            resume_file.unlink()
        handler = {"idle": self._idle, "opening": self._opening, "open": self._open,
                   "closing": self._closing, "rebalancing": self._rebalancing}[self.st["phase"]]
        handler(self.v.now())
        self.store.save()

    def pause(self, reason: str) -> None:
        self.st["paused"] = reason
        log.error("PAUSE: %s (staking actif; `python -m ptbot resume` pour reprendre)", reason)

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
                           "à la main")
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
        transfer = plan_rebalance(bal["A"], bal["B"], self.p, mins)
        if transfer:
            log.info("Soldes déséquilibrés (A=%.2f$, B=%.2f$): transfert de %.2f$ de %s vers %s",
                     bal["A"], bal["B"], transfer.amount, transfer.src, transfer.dst)
            self.st["rebalance"] = {"src": transfer.src, "dst": transfer.dst, "amount": transfer.amount,
                                    "step": "withdraw", "step_started": now, "warned": now, "next_poll": 0,
                                    "withdraw_attempts": 0, "send_attempts": 0}
            self.st["phase"] = "rebalancing"
            return
        plan = plan_open(bal["A"], bal["B"], *mins, self.p)
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
        cycle = {
            "n": self.st["totals"]["cycles"] + 1, "started": now, "coin": coin,
            "deadline": now + self.t.open_deadline_s + self.t.submit_margin_s,
            "margin": plan.margin, "leverage": plan.leverage, "close_move": plan.close_move,
            "mid_at_open": mid, "mid_hi": mid, "mid_lo": mid, "aborted": False, "failure_counted": False,
            "free_start": sum(bal.values()), "equity_start": equity, "paper_start": paper,
            "legs": {w: {"status": "pending", "intent": None, "pos_id": None, "entry": None, "miss": 0,
                         "gone_reads": 0, "close_sends": 0} for w in WALLETS},
            "next_poll": 0, "close_sent_at": None, "settle_at": None,
        }
        log.info("Cycle #%d: long %s sur A + short %s sur B, marge %.2f$ x%d (notionnel %s$), "
                 "fermeture à ±%.2f%% (liquidation à ±%.2f%%), %s=%.2f",
                 cycle["n"], coin, coin, plan.margin, plan.leverage, f"{plan.notional:,.0f}",
                 plan.close_move * 100, liq_distance(plan.leverage) * 100, coin, mid)
        self.st["cycle"] = cycle
        self.st["phase"] = "opening"
        self.st["intents_valid_until"] = cycle["deadline"] + self.t.indexer_slack_s
        self.store.save()  # write-ahead: a crash during the requests below resumes in 'opening'
        for w in WALLETS:
            leg = cycle["legs"][w]
            try:
                leg["intent"] = self.v.open(w, coin, IS_LONG[w], plan.margin, plan.leverage, self.t.open_deadline_s)
                leg["status"] = "sent"
            except IntentRejected as e:
                leg["status"] = "rejected"
                log.error("Ouverture refusée sur %s: %s", w, e)
                break  # no point opening the other leg alone
            except Exception as e:
                leg["status"] = "unknown"
                log.warning("Ouverture sur %s: pas de réponse claire (%s). L'ordre a peut-être été exécuté, je surveille", w, e)
        for leg in cycle["legs"].values():
            if leg["status"] == "pending":
                leg["status"] = "not_sent"
        # Every order was signed before now, so none can execute after now + its lifetime.
        cycle["deadline"] = self.v.now() + self.t.open_deadline_s
        live = any(leg["status"] in MAYBE_EXECUTED for leg in cycle["legs"].values())
        self.st["intents_valid_until"] = cycle["deadline"] + self.t.indexer_slack_s if live else now
        if any(leg["status"] == "rejected" for leg in cycle["legs"].values()):
            self._open_failed(cycle, "ouverture refusée")

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

    def _sync_legs(self, w: str, listed: dict, mid: float, now: float) -> None:
        """Adopts a new position as the wallet's leg if its order may have executed,
        and closes everything else that is not a leg."""
        c = self.st["cycle"]
        leg = c["legs"][w]
        if leg["pos_id"] is None and leg["status"] in MAYBE_EXECUTED and now < self.st["intents_valid_until"]:
            for p in listed.values():
                if p.id in self._known(w) or p.coin not in (None, c["coin"]) or p.is_long not in (None, IS_LONG[w]):
                    continue
                self._remember(w, p.id)
                leg["pos_id"] = p.id
                # Unknown entry: assume the worst price seen since the order was sent.
                worst = c["mid_hi"] if IS_LONG[w] else c["mid_lo"]
                leg["entry"] = plausible_price(p.entry, mid) or worst
                log.info("Wallet %s: position #%d ouverte (entrée %.2f)", w, p.id, leg["entry"])
                if self.st["phase"] == "closing":   # late fill after an abort: close it with the cycle
                    log.warning("Wallet %s: exécution tardive, je la ferme", w)
                    self._send_close(now, [w], reset_timer=False)
                break
        self._sweep(w, listed, {leg["pos_id"]} - {None}, now)

    def _opening(self, now: float) -> None:
        c = self.st["cycle"]
        if now < c["next_poll"]:
            return
        c["next_poll"] = now + self.t.fast_poll_s
        mid = self.v.mid(c["coin"])
        c["mid_hi"], c["mid_lo"] = max(c["mid_hi"], mid), min(c["mid_lo"], mid)
        found = [w for w in WALLETS if c["legs"][w]["pos_id"] is not None]
        for w in found:  # a lone leg is naked: never let it run past its exit (needs only the price)
            if len(found) < len(WALLETS) and self._adverse(w, mid) >= c["close_move"]:
                log.warning("Jambe %s seule et déjà à %.2f%% de perte: je la ferme", w, self._adverse(w, mid) * 100)
                self._abort(now, found)
                return
        for w in WALLETS:
            try:
                listed = self._read(w)
            except Exception as e:
                log.warning("Positions de %s illisibles (%s), nouvel essai", w, e)
                continue
            self._sync_legs(w, listed, mid, now)
        found = [w for w in WALLETS if c["legs"][w]["pos_id"] is not None]
        if len(found) == len(WALLETS):
            self.st["open_failures"] = 0
            c["next_poll"] = now + self.t.positions_every_s
            c["heartbeat"] = now
            self.st["phase"] = "open"
            ea, eb = c["legs"]["A"]["entry"], c["legs"]["B"]["entry"]
            log.info("Paire ouverte (A %.2f, B %.2f): fermeture sous %.2f ou au-dessus de %.2f",
                     ea, eb, ea * (1 - c["close_move"]), eb * (1 + c["close_move"]))
            return
        waiting = [w for w in WALLETS if c["legs"][w]["pos_id"] is None and c["legs"][w]["status"] in MAYBE_EXECUTED]
        if waiting and now < self.st["intents_valid_until"]:
            return
        if found:
            log.warning("Une seule jambe ouverte (%s): je la ferme pour rester neutre", found)
            self._abort(now, found)
            if waiting:
                self._open_failed(c, "une jambe jamais exécutée")
            return
        if not waiting:  # every leg was refused outright (already counted)
            self.st["cycle"] = None
            self.st["phase"] = "idle"
            return
        free_now = sum(self.v.usdc(w) for w in WALLETS)   # read before changing the state
        self.st["cycle"] = None
        self.st["phase"] = "idle"
        if c["free_start"] - free_now >= 0.5 * c["margin"]:
            self.pause("les soldes ont baissé mais aucune position n'est visible via l'API: des positions "
                       "sont sans doute ouvertes. Vérifiez sur le site et lancez `python -m ptbot probe`")
            return
        log.warning("Aucune jambe exécutée avant l'expiration des ordres")
        self._open_failed(c, "ordres acceptés mais jamais exécutés")

    def _abort(self, now: float, wallets: list[str]) -> None:
        self.st["cycle"]["aborted"] = True
        self._send_close(now, wallets)
        self.st["phase"] = "closing"

    # --- open: watch the price, close both legs before liquidation --------
    def _open(self, now: float) -> None:
        c = self.st["cycle"]
        mid = self.v.mid(c["coin"])
        worst = max(self._adverse(w, mid) for w in WALLETS)
        if worst >= c["close_move"] - 1e-12:
            log.info("%s=%.2f: une jambe perd %.3f%%, fermeture des deux jambes", c["coin"], mid, worst * 100)
            self._send_close(now, list(WALLETS))
            self.st["phase"] = "closing"
            return
        if now >= c["next_poll"]:
            listed = {w: self._read(w) for w in WALLETS}
            c["next_poll"] = now + self.t.positions_every_s
            for w in WALLETS:
                self._sweep(w, listed[w], {c["legs"][w]["pos_id"]}, now)
            for w in WALLETS:
                leg = c["legs"][w]
                if leg["pos_id"] in listed[w]:
                    leg["miss"] = 0
                    continue
                leg["miss"] += 1
                if leg["miss"] >= self.t.missing_reads or self._adverse(w, mid) >= liq_distance(c["leverage"]):
                    log.warning("Jambe %s disparue (liquidée ou fermée à la main): je ferme la paire", w)
                    self._send_close(now, list(WALLETS))  # both: if the "missing" one still exists, close it too
                    self.st["phase"] = "closing"
                    return
                c["next_poll"] = now + self.t.fast_poll_s  # one miss may be a glitch: re-check quickly
        if now - c.get("heartbeat", 0) >= self.t.heartbeat_s:
            c["heartbeat"] = now
            log.info("Paire ouverte: %s=%.2f, jambe la plus en perte %.3f%% (fermeture à %.2f%%)",
                     c["coin"], mid, worst * 100, c["close_move"] * 100)

    def _send_close(self, now: float, wallets: list[str], reset_timer: bool = True) -> None:
        c = self.st["cycle"]
        for w in wallets:
            leg = c["legs"][w]
            if leg["pos_id"] is None:
                continue
            leg["close_sends"] += 1
            try:
                self.v.close(w, [leg["pos_id"]])
            except Exception as e:
                log.error("Fermeture #%s sur %s refusée: %s (nouvel essai)", leg["pos_id"], w, e)
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
        listed = {w: self._read(w) for w in WALLETS}
        for w in WALLETS:
            self._sync_legs(w, listed[w], mid, now)
        visible, unconfirmed = [], []
        for w in WALLETS:
            leg = c["legs"][w]
            if leg["pos_id"] is None:
                continue
            if leg["pos_id"] in listed[w]:
                leg["gone_reads"] = 0
                visible.append(w)
            else:
                leg["gone_reads"] += 1
                if leg["gone_reads"] < self.t.missing_reads:
                    unconfirmed.append(w)
        waiting = [w for w in WALLETS if c["legs"][w]["pos_id"] is None
                   and c["legs"][w]["status"] in MAYBE_EXECUTED and now < self.st["intents_valid_until"]]
        if visible:
            sends = max(c["legs"][w]["close_sends"] for w in visible)
            urgent = any(self._adverse(w, mid) >= (c["close_move"] + liq_distance(c["leverage"])) / 2 for w in visible)
            interval = self.t.close_retry_s if sends < self.t.max_close_sends else self.t.ghost_retry_s
            if urgent or now - c["close_sent_at"] >= interval:
                log.warning("Toujours ouvert sur %s: je renvoie la fermeture", visible)
                self._send_close(now, visible)
            if sends >= self.t.max_close_sends and not c.get("ghost_paused"):
                c["ghost_paused"] = True
                self.pause(f"positions {[c['legs'][w]['pos_id'] for w in visible]} toujours listées après "
                           f"{sends} fermetures. Si le site les montre fermées, l'API est mal lue; sinon fermez-les "
                           "à la main")
        if visible or unconfirmed or waiting:
            c["settle_at"] = None
            return
        if c["settle_at"] is None:
            c["settle_at"] = now + self.t.settle_s
            return
        if now >= c["settle_at"]:
            self._book_cycle(now)

    def _book_cycle(self, now: float) -> None:
        c = self.st["cycle"]
        free = sum(self.v.usdc(w) for w in WALLETS)
        queued = sum(self.v.queued(w) for w in WALLETS)
        paper = (sum(self.v.paper_raw(w) for w in WALLETS) - c["paper_start"]) / SCALE
        legs_opened = sum(1 for leg in c["legs"].values() if leg["pos_id"] is not None)
        returned = free - (c["free_start"] - legs_opened * c["margin"])
        self.st["cycle"] = None
        self.st["phase"] = "idle"
        self.st["next_try"] = 0
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
        tot["paper_minted"] += paper
        ea, eb = c["legs"]["A"]["entry"], c["legs"]["B"]["entry"]
        skewed = bool(ea and eb and abs(ea / eb - 1) > 0.002)  # legs filled far apart: not a normal cycle
        kind = "aborted" if c["aborted"] else "skewed" if skewed else "normal"
        self.st["cycles_all"].append({"ts": now, "usd": usd, "paper": paper, "kind": kind})
        del self.st["cycles_all"][:-1000]
        if kind == "normal":
            self.st["history"].append({"ts": now, "usd": usd, "paper": paper})
            del self.st["history"][:-1000]
        note = {"aborted": "(avorté) ", "skewed": "(entrées décalées) ", "normal": ""}[kind]
        log.info("Cycle #%d %sterminé en %.0f min: coût %.2f$, +%s PAPER (%.2f$/1000). Total: %s PAPER pour %.2f$",
                 c["n"], note, (now - c["started"]) / 60, usd, f"{paper:,.0f}",
                 cost_per_1000(usd, paper), f"{tot['paper_minted']:,.0f}", tot["usd_spent"])
        hit, avg = cost_cap_hit(self.st["history"], self.p)
        everything = self.st["cycles_all"]
        overall = cost_per_1000(sum(x["usd"] for x in everything), sum(x["paper"] for x in everything))
        if hit:
            span = "le dernier cycle" if self.p.cost_window <= 1 else f"les {self.p.cost_window} derniers cycles"
            self.pause(f"coût {avg:.2f}$/1000 PAPER sur {span} > plafond {self.p.max_cost_per_1000:.2f}$: "
                       "farming arrêté")
        elif overall > self.p.max_cost_per_1000 and (
                len(everything) >= 3 or tot["usd_spent"] >= 0.25 * self.st.get("equity_first", float("inf"))):
            self.pause(f"coût cumulé {overall:.2f}$/1000 PAPER (cycles avortés compris) > plafond "
                       f"{self.p.max_cost_per_1000:.2f}$: farming arrêté")

    # --- staking --------------------------------------------------------
    def _track_stakes(self, now: float) -> None:
        pending = self.st["pending_stakes"]
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
            log.error("Wallet %s: 3 stakes en échec, staking automatique suspendu %.0f h pour ce wallet "
                      "(`python -m ptbot stake-now` ou le site). Le farming continue", w, self.t.stake_disable_s / 3600)

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
            spot, perp, src_before = self.v.core_spot_usdc(src), self.v.core_perp_usdc(src), self.v.usdc(src)
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
            if waited > WITHDRAW_DEADLINE_S + self.t.indexer_slack_s and self.v.usdc(src) >= r["src_before"] - 0.5:
                log.warning("Rééquilibrage: le retrait a expiré sans être exécuté, nouvel essai")
                self._set_step(r, "withdraw", now)
                return
            self._rebalance_wait(r, now, "l'arrivée des USDC sur HyperCore")
        elif r["step"] == "send":
            if r["send_amount"] <= 0:
                self._finish_rebalance(f"rien à envoyer vers {dst}: vérifiez le spot HyperCore de {src}")
                return
            if r["send_attempts"] >= 3:
                self._finish_rebalance(f"envoi HyperCore {src} -> dépôt {dst} en échec 3 fois: "
                                       f"{r['send_amount']:.2f}$ sont sur le spot HyperCore de {src}")
                return
            dest = self.v.deposit_address(dst)
            if "dst_before" not in r:  # first attempt only: a retry must compare with the original balances
                r["dst_before"], r["spot_before_send"] = self.v.usdc(dst), self.v.core_spot_usdc(src)
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
            if _arrived(self.v.usdc(dst) - r["dst_before"], r["send_amount"]):
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
