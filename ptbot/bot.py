"""Delta-neutral state machine: idle -> opening -> open -> closing -> idle,
with a `rebalancing` detour when the two USDC balances drift apart.

State is saved after every step (data/state.json), so a restart resumes the
current cycle instead of opening a new pair on top of it.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .constants import SCALE
from .positions import plausible_price
from .strategy import (Params, Skip, cost_cap_hit, cost_per_1000, floor_cents, plan_open,
                       plan_rebalance, price_move, should_exit)

log = logging.getLogger("ptbot")

WALLETS = ("A", "B")
IS_LONG = {"A": True, "B": False}   # A carries the long leg, B the short leg


@dataclass
class Timing:
    poll_s: float = 2.0             # main loop period
    positions_every_s: float = 10.0  # leg checks while the pair is open
    fast_poll_s: float = 3.0        # position polling while opening/closing
    stake_every_s: float = 30.0
    open_deadline_s: int = 120      # signed open intents expire after this
    open_timeout_s: float = 240.0   # > deadline: no leg can execute after it
    close_retry_s: float = 45.0
    settle_s: float = 5.0
    stake_confirm_s: float = 60.0
    idle_retry_s: float = 60.0
    rebalance_poll_s: float = 10.0
    rebalance_timeout_s: float = 1800.0
    heartbeat_s: float = 60.0


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {"phase": "idle", "cycle": None, "rebalance": None, "paused": None, "history": [],
                    "totals": {"cycles": 0, "usd_spent": 0.0, "paper_minted": 0.0, "paper_staked": 0.0}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)


class Bot:
    def __init__(self, venue, params: Params, store: StateStore, timing: Timing | None = None) -> None:
        self.v = venue
        self.p = params
        self.store = store
        self.st = store.data
        self.t = timing or Timing()
        self._last_stake_check = 0.0
        self._last_idle_msg = ""

    # --- main loop -------------------------------------------------------
    def run(self, max_steps: int | None = None, stop_file: Path | None = None) -> None:
        steps = errors = 0
        while max_steps is None or steps < max_steps:
            if stop_file and stop_file.exists():
                log.warning("Fichier %s présent: arrêt du bot", stop_file)
                return
            try:
                self.step()
                errors = 0
            except Exception:
                errors += 1
                log.exception("Erreur pendant le cycle (nouvel essai au prochain tour)")
                self.v.sleep(min(5 * errors, 60))   # back off while an API is down
            self.v.sleep(self.t.poll_s)
            steps += 1

    def step(self) -> None:
        handler = {"idle": self._idle, "opening": self._opening, "open": self._open,
                   "closing": self._closing, "rebalancing": self._rebalancing}[self.st["phase"]]
        handler(self.v.now())
        self.store.save()

    def pause(self, reason: str) -> None:
        self.st["paused"] = reason
        log.error("PAUSE: %s (staking actif; `python -m ptbot resume` pour reprendre)", reason)

    def _say_once(self, msg: str) -> None:
        if msg != self._last_idle_msg:
            log.info(msg)
            self._last_idle_msg = msg

    # --- idle: stake, rebalance, open the next pair ------------------------
    def _idle(self, now: float) -> None:
        self.stake_if_needed(now)
        if now < self.st.get("next_try", 0):
            return
        self.st["next_try"] = now + self.t.idle_retry_s
        if self.st.get("paused"):
            self._say_once(f"En pause: {self.st['paused']}")
            return
        if self.v.trading_paused():
            self._say_once("Trading en pause côté PaperTrade, j'attends")
            return
        for w in WALLETS:
            stray = self.v.positions(w)
            if stray:
                self._say_once(f"Wallet {w}: positions déjà ouvertes {[p.id for p in stray]}. "
                               "Fermez-les (`python -m ptbot close-all`) pour que le bot démarre un cycle")
                return
        bal = {w: self.v.usdc(w) for w in WALLETS}
        transfer = plan_rebalance(bal["A"], bal["B"], self.p)
        if transfer:
            log.info("Soldes déséquilibrés (A=%.2f$, B=%.2f$): transfert de %.2f$ de %s vers %s",
                     bal["A"], bal["B"], transfer.amount, transfer.src, transfer.dst)
            self.st["rebalance"] = {"src": transfer.src, "dst": transfer.dst, "amount": transfer.amount,
                                    "step": "withdraw", "started": now, "next_poll": 0}
            self.st["phase"] = "rebalancing"
            return
        plan = plan_open(bal["A"], bal["B"], *self.v.minimums(), self.p)
        if isinstance(plan, Skip):
            self._say_once(f"Pas de cycle: {plan.reason}")
            return
        self._open_pair(now, plan, bal)

    def _open_pair(self, now: float, plan, bal: dict) -> None:
        coin = self.p.coin
        mid = self.v.mid(coin)
        cycle = {
            "n": self.st["totals"]["cycles"] + 1, "started": now, "coin": coin,
            "margin": plan.margin, "leverage": plan.leverage, "close_move": plan.close_move,
            "mid_at_open": mid, "entry": None, "failed": False, "aborted": False,
            "free_start": sum(bal.values()),
            "equity_start": sum(bal.values()) + sum(self.v.queued(w) for w in WALLETS),
            "paper_start": sum(self.v.paper_raw(w) for w in WALLETS),
            "legs": {}, "next_poll": 0, "close_sent_at": None, "settle_at": None,
        }
        log.info("Cycle #%d: long %s sur A + short %s sur B, marge %.2f$ x%d (notionnel %s$), "
                 "fermeture à ±%.2f%% (liquidation à ±%.2f%%), %s=%.2f",
                 cycle["n"], coin, coin, plan.margin, plan.leverage, f"{plan.notional:,.0f}",
                 plan.close_move * 100, (1 / plan.leverage - 0.0005) * 100, coin, mid)
        for w in WALLETS:
            try:
                intent = self.v.open(w, coin, IS_LONG[w], plan.margin, plan.leverage, self.t.open_deadline_s)
            except Exception as e:
                log.error("Ouverture refusée sur %s: %s", w, e)
                cycle["failed"] = True
                break
            cycle["legs"][w] = {"intent": intent, "pos_id": None, "entry": None}
        self.st["cycle"] = cycle
        self.st["phase"] = "opening"
        if cycle["failed"]:
            self.st["open_failures"] = self.st.get("open_failures", 0) + 1
            if not cycle["legs"]:
                self.st["cycle"] = None
                self.st["phase"] = "idle"
            if self.st["open_failures"] >= 5:
                self.pause("5 ouvertures refusées d'affilée: vérifiez les logs (signature, session key, solde)")
        else:
            self.st["open_failures"] = 0

    # --- opening: wait until both legs exist -------------------------------
    def _opening(self, now: float) -> None:
        c = self.st["cycle"]
        if now < c["next_poll"]:
            return
        c["next_poll"] = now + self.t.fast_poll_s
        mid = self.v.mid(c["coin"])
        for w in WALLETS:  # also wallets whose submit errored: the intent may still have executed
            leg = c["legs"].setdefault(w, {"intent": None, "pos_id": None, "entry": None})
            if leg["pos_id"] is not None:
                continue
            for p in self.v.positions(w):
                if p.coin in (None, c["coin"]) and p.is_long in (None, IS_LONG[w]):
                    leg["pos_id"] = p.id
                    leg["entry"] = plausible_price(p.entry, mid)
                    log.info("Wallet %s: position #%d ouverte", w, p.id)
                    break
        found = [w for w, leg in c["legs"].items() if leg["pos_id"] is not None]
        if len(found) == len(WALLETS):
            entries = [c["legs"][w]["entry"] for w in WALLETS if c["legs"][w]["entry"]]
            c["entry"] = sum(entries) / len(entries) if entries else mid
            c["next_poll"] = now + self.t.positions_every_s
            c["heartbeat"] = now
            self.st["phase"] = "open"
            log.info("Paire ouverte, prix d'entrée %.2f: fermeture sous %.2f ou au-dessus de %.2f",
                     c["entry"], c["entry"] * (1 - c["close_move"]), c["entry"] * (1 + c["close_move"]))
            return
        if c["failed"] and found:
            log.warning("Une seule jambe ouverte (%s): je la ferme pour rester neutre", found)
            self._abort(now, found)
        elif now - c["started"] > self.t.open_timeout_s:
            if found:
                log.warning("Délai dépassé avec une seule jambe (%s): je la ferme", found)
                self._abort(now, found)
                return
            self.st["cycle"] = None
            self.st["phase"] = "idle"
            if c["free_start"] - sum(self.v.usdc(w) for w in WALLETS) >= 0.5 * c["margin"]:
                self.pause("les soldes ont baissé mais aucune position n'est visible via l'API: "
                           "des positions sont sans doute ouvertes. Vérifiez sur le site et lancez "
                           "`python -m ptbot probe` (lecture des positions à ajuster)")
            else:
                log.warning("Aucune jambe exécutée avant l'expiration des intents: retour en attente")

    def _abort(self, now: float, wallets: list[str]) -> None:
        c = self.st["cycle"]
        c["aborted"] = True
        self._send_close(now, wallets)
        self.st["phase"] = "closing"

    # --- open: watch the price, close both legs before liquidation --------
    def _open(self, now: float) -> None:
        c = self.st["cycle"]
        mid = self.v.mid(c["coin"])
        move = price_move(c["entry"], mid)
        if should_exit(c["entry"], mid, c["close_move"]):
            log.info("Prix %.2f (%+.3f%%): fermeture des deux jambes", mid, move * 100)
            self._send_close(now, list(WALLETS))
            self.st["phase"] = "closing"
            return
        if now >= c["next_poll"]:
            c["next_poll"] = now + self.t.positions_every_s
            for w in WALLETS:
                if c["legs"][w]["pos_id"] not in {p.id for p in self.v.positions(w)}:
                    others = [o for o in WALLETS if o != w]
                    log.warning("Jambe %s disparue (liquidée ou fermée à la main): je ferme %s", w, others)
                    self._send_close(now, others)
                    self.st["phase"] = "closing"
                    return
        if now - c.get("heartbeat", 0) >= self.t.heartbeat_s:
            c["heartbeat"] = now
            log.info("Paire ouverte: %s=%.2f, %+.3f%% (fermeture à ±%.2f%%)",
                     c["coin"], mid, move * 100, c["close_move"] * 100)

    def _send_close(self, now: float, wallets: list[str]) -> None:
        c = self.st["cycle"]
        for w in wallets:
            pid = c["legs"].get(w, {}).get("pos_id")
            if pid is None:
                continue
            try:
                self.v.close(w, [pid])
            except Exception as e:
                log.error("Fermeture #%s sur %s refusée: %s (nouvel essai)", pid, w, e)
        c["close_sent_at"] = now
        c["next_poll"] = now + self.t.fast_poll_s

    # --- closing: wait for both legs to be gone, then book the cycle --------
    def _closing(self, now: float) -> None:
        c = self.st["cycle"]
        if now < c["next_poll"]:
            return
        c["next_poll"] = now + self.t.fast_poll_s
        remaining = [w for w, leg in c["legs"].items()
                     if leg["pos_id"] is not None and leg["pos_id"] in {p.id for p in self.v.positions(w)}]
        if remaining:
            c["settle_at"] = None
            if now - c["close_sent_at"] >= self.t.close_retry_s:
                log.warning("Toujours ouvert sur %s: je renvoie la fermeture", remaining)
                self._send_close(now, remaining)
            return
        if c["settle_at"] is None:
            c["settle_at"] = now + self.t.settle_s
            return
        if now >= c["settle_at"]:
            self._book_cycle(now)

    def _book_cycle(self, now: float) -> None:
        c = self.st["cycle"]
        free = sum(self.v.usdc(w) for w in WALLETS)
        legs_opened = sum(1 for leg in c["legs"].values() if leg["pos_id"] is not None)
        free_while_open = c["free_start"] - legs_opened * c["margin"]
        if legs_opened and free - free_while_open < 0.5 * c["margin"]:
            self.st["phase"] = "idle"
            self.st["cycle"] = None
            self.pause("les positions ne sont plus visibles via l'API mais aucune marge n'est revenue: "
                       "elles sont peut-être encore ouvertes. Vérifiez sur le site avant `resume`")
            return
        equity = free + sum(self.v.queued(w) for w in WALLETS)
        usd = c["equity_start"] - equity
        paper = (sum(self.v.paper_raw(w) for w in WALLETS) - c["paper_start"]) / SCALE
        tot = self.st["totals"]
        tot["cycles"] += 1
        tot["usd_spent"] += usd
        tot["paper_minted"] += paper
        if not c["aborted"]:
            self.st["history"].append({"ts": now, "usd": usd, "paper": paper})
        log.info("Cycle #%d terminé en %.0f min: coût %.2f$, +%s PAPER (%.2f$/1000). Total: %s PAPER pour %.2f$",
                 c["n"], (now - c["started"]) / 60, usd, f"{paper:,.0f}", cost_per_1000(usd, paper),
                 f"{tot['paper_minted']:,.0f}", tot["usd_spent"])
        self.st["cycle"] = None
        self.st["phase"] = "idle"
        self.st["next_try"] = 0
        hit, avg = cost_cap_hit(self.st["history"], self.p)
        if hit:
            self.pause(f"coût moyen {avg:.2f}$/1000 PAPER sur les {self.p.cost_window} derniers cycles "
                       f"> plafond {self.p.max_cost_per_1000:.2f}$: farming arrêté")

    # --- staking --------------------------------------------------------
    def stake_if_needed(self, now: float, force: bool = False) -> None:
        """Stakes each wallet's whole unstaked PAPER once it reaches the threshold.
        Only called between cycles: PAPER is minted when positions close."""
        if not force and now - self._last_stake_check < self.t.stake_every_s:
            return
        self._last_stake_check = now
        threshold = int(self.p.stake_threshold * SCALE)
        for w in WALLETS:
            raw = self.v.paper_raw(w)
            if raw <= 0 or raw < threshold:
                continue
            try:
                self.v.stake(w, raw)
            except Exception as e:
                log.error("Stake refusé sur %s: %s", w, e)
                continue
            log.info("Wallet %s: stake de %s PAPER", w, f"{raw / SCALE:,.0f}")
            deadline = self.v.now() + self.t.stake_confirm_s
            while self.v.paper_raw(w) >= raw and self.v.now() < deadline:
                self.v.sleep(3)
            if self.v.paper_raw(w) >= raw:
                log.warning("Wallet %s: stake pas encore exécuté par le relayer", w)
            self.st["totals"]["paper_staked"] += raw / SCALE

    # --- rebalancing: A <-> B through HyperCore -----------------------------
    def _rebalancing(self, now: float) -> None:
        r = self.st["rebalance"]
        if now < r["next_poll"]:
            return
        r["next_poll"] = now + self.t.rebalance_poll_s
        src, dst, amount = r["src"], r["dst"], r["amount"]
        if r["step"] != "withdraw" and now - r["started"] > self.t.rebalance_timeout_s:
            self._finish_rebalance(f"rééquilibrage bloqué à l'étape '{r['step']}' depuis "
                                   f"{self.t.rebalance_timeout_s / 60:.0f} min: vérifiez les soldes à la main")
            return
        if r["step"] == "withdraw":
            if not self.v.deposit_address(dst):
                self._finish_rebalance(f"adresse de dépôt PaperTrade du wallet {dst} manquante "
                                       f"(DEPOSIT_ADDRESS_{dst} dans .env)")
                return
            r["spot_before"] = self.v.core_spot_usdc(src)
            r["perp_before"] = self.v.core_perp_usdc(src)
            try:
                self.v.withdraw_to_core(src, amount)
            except Exception as e:
                self._finish_rebalance(f"retrait de {amount:.2f}$ depuis {src} refusé: {e}")
                return
            log.info("Rééquilibrage: retrait de %.2f$ de PaperTrade %s vers HyperCore", amount, src)
            r["step"] = "await_core"
        elif r["step"] == "await_core":
            received = self.v.core_spot_usdc(src) - r["spot_before"]
            if received >= min(amount * 0.95, amount - 1.5):
                r["send_amount"] = floor_cents(min(amount, received))
                r["step"] = "send"
                r["next_poll"] = 0
            elif not r.get("moved_from_perp") and self.v.core_perp_usdc(src) - r["perp_before"] >= amount * 0.98:
                log.info("Rééquilibrage: USDC arrivés en perp sur HyperCore, passage en spot")
                self.v.core_perp_to_spot(src, amount)
                r["moved_from_perp"] = True
        elif r["step"] == "send":
            dest = self.v.deposit_address(dst)
            r["dst_before"] = self.v.usdc(dst)
            try:
                self.v.core_spot_send(src, dest, r["send_amount"])
            except Exception as e:
                self._finish_rebalance(f"envoi HyperCore {src} -> dépôt {dst} en échec: {e}")
                return
            log.info("Rééquilibrage: %.2f$ envoyés vers l'adresse de dépôt de %s (%s)", r["send_amount"], dst, dest)
            r["step"] = "await_credit"
        elif r["step"] == "await_credit":
            credited = self.v.usdc(dst) - r["dst_before"]
            if credited >= min(r["send_amount"] * 0.95, r["send_amount"] - 1.5):
                log.info("Rééquilibrage terminé: A=%.2f$, B=%.2f$", self.v.usdc("A"), self.v.usdc("B"))
                self._finish_rebalance(None)

    def _finish_rebalance(self, error: str | None) -> None:
        self.st["rebalance"] = None
        self.st["phase"] = "idle"
        self.st["next_try"] = 0
        if error:
            self.pause(error)
