"""Delta-neutral state machine: idle -> opening -> open -> closing -> idle,
with a `rebalancing` detour when the two USDC balances drift apart.

Safety rules the code below relies on:
  - an order whose submit failed ambiguously (timeout, lost response) may still
    execute until its signed deadline, so the bot keeps watching for it and never
    opens a new pair before that deadline has passed;
  - any position the bot does not manage is closed (the wallets are dedicated);
  - state is saved before every irreversible request and after every step, so a
    crash or restart resumes where it was instead of acting twice.
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
from .positions import plausible_price
from .strategy import (Params, Skip, cost_cap_hit, cost_per_1000, floor_cents, liq_distance, plan_open,
                       plan_rebalance)

log = logging.getLogger("ptbot")

WALLETS = ("A", "B")
IS_LONG = {"A": True, "B": False}   # A carries the long leg, B the short leg
# Leg statuses whose order may still execute ('pending' = state saved before the request, then a crash).
MAYBE_EXECUTED = ("sent", "unknown", "pending")


@dataclass
class Timing:
    poll_s: float = 2.0              # main loop period
    positions_every_s: float = 10.0  # leg checks while the pair is open
    fast_poll_s: float = 3.0         # polling while opening/closing or after a missed read
    missing_reads: int = 3           # consecutive misses before a leg counts as gone
    stake_every_s: float = 30.0
    open_deadline_s: int = 120       # signed open intents expire after this
    indexer_slack_s: float = 30.0    # time for an executed order to show up in the API
    close_retry_s: float = 12.0
    settle_s: float = 5.0
    idle_retry_s: float = 60.0
    stray_retry_s: float = 15.0
    rebalance_poll_s: float = 10.0
    rebalance_warn_s: float = 900.0
    rebalance_give_up_s: float = 7200.0
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


def _arrived(delta: float, amount: float) -> bool:
    """A transfer counts as arrived net of small fees."""
    return delta >= min(amount * 0.95, amount - 1.5)


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
        self.st.setdefault("seen_ids", {w: [] for w in WALLETS})
        self.st.setdefault("pending_stakes", {})
        self.st.setdefault("stake_failures", {})
        self.st.setdefault("open_failures", 0)
        self.st.setdefault("intents_valid_until", 0)

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

    def _mark_seen(self, w: str, pid: int) -> None:
        seen = self.st["seen_ids"].setdefault(w, [])
        if pid not in seen:
            seen.append(pid)
            del seen[:-200]

    # --- idle: stake, clean up, rebalance, open the next pair ----------------
    def _idle(self, now: float) -> None:
        self._track_stakes(now)
        self.stake_if_needed(now)
        if now < self.st.get("next_try", 0):
            return
        self.st["next_try"] = now + self.t.idle_retry_s
        if self._close_strays(now):
            return
        if self.st.get("paused"):
            self._say_once(f"En pause: {self.st['paused']}")
            return
        if self.v.trading_paused():
            self._say_once("Trading en pause côté PaperTrade, j'attends")
            return
        if now < self.st["intents_valid_until"]:
            self._say_once("J'attends l'expiration des ordres du cycle précédent avant d'en ouvrir d'autres")
            self.st["next_try"] = self.st["intents_valid_until"]
            return
        if self.st["pending_stakes"]:
            self.st["next_try"] = now + self.t.fast_poll_s
            return
        bal = {w: self.v.usdc(w) for w in WALLETS}
        mins = self.v.minimums()
        transfer = plan_rebalance(bal["A"], bal["B"], self.p, mins[1])
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

    def _close_strays(self, now: float) -> bool:
        """Closes any open position outside a cycle (late fill, lost response, manual
        trade). Returns True while some exist, so no new pair is opened on top."""
        found = False
        attempts = self.st.setdefault("stray_attempts", {})
        listed = {}
        for w in WALLETS:
            ids = [p.id for p in self.v.positions(w)]
            if not ids:
                continue
            found = True
            for pid in ids:
                self._mark_seen(w, pid)
                key = f"{w}:{pid}"
                listed[key] = attempts.get(key, 0) + 1
            worst = max(listed[f"{w}:{pid}"] for pid in ids)
            if worst <= 3 or worst % 20 == 0:
                log.warning("Wallet %s: positions non gérées %s, je les ferme (wallets dédiés au bot)", w, ids)
            if worst == 8:
                self.pause(f"wallet {w}: positions {ids} toujours listées après 8 fermetures. Si le site les montre "
                           "fermées, l'API est mal lue (`python -m ptbot probe`); sinon fermez-les à la main")
            for i in range(0, len(ids), 25):
                try:
                    self.v.close(w, ids[i:i + 25])
                except Exception as e:
                    log.error("Fermeture de %s sur %s refusée: %s", ids[i:i + 25], w, e)
        self.st["stray_attempts"] = listed
        if found:
            self.st["next_try"] = now + self.t.stray_retry_s
        return found

    def _open_pair(self, now: float, plan, bal: dict) -> None:
        coin = self.p.coin
        mid = self.v.mid(coin)
        deadline = now + self.t.open_deadline_s
        cycle = {
            "n": self.st["totals"]["cycles"] + 1, "started": now, "coin": coin, "deadline": deadline,
            "margin": plan.margin, "leverage": plan.leverage, "close_move": plan.close_move,
            "mid_at_open": mid, "aborted": False,
            "free_start": sum(bal.values()),
            "equity_start": sum(bal.values()) + sum(self.v.queued(w) for w in WALLETS),
            "paper_start": sum(self.v.paper_raw(w) for w in WALLETS),
            "legs": {w: {"status": "pending", "intent": None, "pos_id": None, "entry": None, "miss": 0}
                     for w in WALLETS},
            "next_poll": 0, "close_sent_at": None, "settle_at": None,
        }
        log.info("Cycle #%d: long %s sur A + short %s sur B, marge %.2f$ x%d (notionnel %s$), "
                 "fermeture à ±%.2f%% (liquidation à ±%.2f%%), %s=%.2f",
                 cycle["n"], coin, coin, plan.margin, plan.leverage, f"{plan.notional:,.0f}",
                 plan.close_move * 100, liq_distance(plan.leverage) * 100, coin, mid)
        self.st["cycle"] = cycle
        self.st["phase"] = "opening"
        self.st["intents_valid_until"] = deadline + self.t.indexer_slack_s
        self.store.save()  # write-ahead: a crash after the requests below resumes in 'opening'
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
                log.warning("Ouverture sur %s: pas de réponse (%s). L'ordre a peut-être été exécuté, je surveille", w, e)
        for leg in cycle["legs"].values():
            if leg["status"] == "pending":
                leg["status"] = "not_sent"
        if any(leg["status"] == "rejected" for leg in cycle["legs"].values()):
            self._open_failed("ouverture refusée")

    # --- opening: wait until both legs exist -------------------------------
    def _adverse(self, w: str, mid: float) -> float:
        entry = self.st["cycle"]["legs"][w]["entry"]
        return 1 - mid / entry if IS_LONG[w] else mid / entry - 1

    def _adopt(self, w: str, mid: float) -> None:
        """Takes the first position not seen before on this wallet as the cycle's leg;
        any other new one is an unwanted duplicate and is closed at once."""
        c = self.st["cycle"]
        leg = c["legs"][w]
        extras = []
        for p in self.v.positions(w):
            if p.id in self.st["seen_ids"].get(w, []):
                continue
            self._mark_seen(w, p.id)
            if leg["pos_id"] is None and p.coin in (None, c["coin"]) and p.is_long in (None, IS_LONG[w]):
                leg["pos_id"] = p.id
                leg["entry"] = plausible_price(p.entry, mid) or mid
                log.info("Wallet %s: position #%d ouverte (entrée %.2f)", w, p.id, leg["entry"])
            else:
                extras.append(p.id)
        if extras:
            log.warning("Wallet %s: positions en trop %s, je les ferme", w, extras)
            try:
                self.v.close(w, extras)
            except Exception as e:
                log.error("Fermeture de %s sur %s refusée: %s", extras, w, e)

    def _opening(self, now: float) -> None:
        c = self.st["cycle"]
        if now < c["next_poll"]:
            return
        c["next_poll"] = now + self.t.fast_poll_s
        mid = self.v.mid(c["coin"])
        for w in WALLETS:
            leg = c["legs"][w]
            if leg["pos_id"] is None and leg["status"] in MAYBE_EXECUTED:
                self._adopt(w, mid)
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
        for w in found:  # a lone leg is naked: never let it run past its exit
            if self._adverse(w, mid) >= c["close_move"]:
                log.warning("Jambe %s seule et déjà à %.2f%% de perte: je la ferme", w, self._adverse(w, mid) * 100)
                self._abort(now, found)
                return
        waiting = [w for w in WALLETS if c["legs"][w]["pos_id"] is None and c["legs"][w]["status"] in MAYBE_EXECUTED]
        if waiting and now < c["deadline"] + self.t.indexer_slack_s:
            return
        if found:
            log.warning("Une seule jambe ouverte (%s): je la ferme pour rester neutre", found)
            self._abort(now, found)
            if waiting:
                self._open_failed("une jambe jamais exécutée")
            return
        self.st["cycle"] = None
        self.st["phase"] = "idle"
        if not waiting:
            return  # every leg was refused outright (already counted)
        if c["free_start"] - sum(self.v.usdc(w) for w in WALLETS) >= 0.5 * c["margin"]:
            self.pause("les soldes ont baissé mais aucune position n'est visible via l'API: des positions "
                       "sont sans doute ouvertes. Vérifiez sur le site et lancez `python -m ptbot probe`")
            return
        log.warning("Aucune jambe exécutée avant l'expiration des ordres")
        self._open_failed("ordres acceptés mais jamais exécutés")

    def _open_failed(self, what: str) -> None:
        self.st["open_failures"] += 1
        if self.st["open_failures"] >= 3:
            self.pause(f"3 paires ratées d'affilée (dernière: {what}). Levier, taille, signature ou session key "
                       "refusés ? Vérifiez les logs et le site")

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
            c["next_poll"] = now + self.t.positions_every_s
            for w in WALLETS:
                leg = c["legs"][w]
                if leg["pos_id"] in {p.id for p in self.v.positions(w)}:
                    leg["miss"] = 0
                    continue
                leg["miss"] += 1
                liquidated = self._adverse(w, mid) >= liq_distance(c["leverage"])
                if leg["miss"] >= self.t.missing_reads or liquidated:
                    others = [o for o in WALLETS if o != w]
                    log.warning("Jambe %s disparue (liquidée ou fermée à la main): je ferme %s", w, others)
                    self._send_close(now, others)
                    self.st["phase"] = "closing"
                    return
                c["next_poll"] = now + self.t.fast_poll_s  # one miss may be a glitch: re-check quickly
        if now - c.get("heartbeat", 0) >= self.t.heartbeat_s:
            c["heartbeat"] = now
            log.info("Paire ouverte: %s=%.2f, jambe la plus en perte %.3f%% (fermeture à %.2f%%)",
                     c["coin"], mid, worst * 100, c["close_move"] * 100)

    def _send_close(self, now: float, wallets: list[str]) -> None:
        c = self.st["cycle"]
        for w in wallets:
            pid = c["legs"][w]["pos_id"]
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
        visible, unconfirmed = [], []
        for w in WALLETS:
            leg = c["legs"][w]
            if leg["pos_id"] is None:
                continue
            if leg["pos_id"] in {p.id for p in self.v.positions(w)}:
                leg["gone_reads"] = 0
                visible.append(w)
            else:
                leg["gone_reads"] = leg.get("gone_reads", 0) + 1
                if leg["gone_reads"] < 2:   # confirm on a second read
                    unconfirmed.append(w)
        if visible or unconfirmed:
            c["settle_at"] = None
            if visible and now - c["close_sent_at"] >= self.t.close_retry_s:
                log.warning("Toujours ouvert sur %s: je renvoie la fermeture", visible)
                self._send_close(now, visible)
            return
        if c["settle_at"] is None:
            c["settle_at"] = now + self.t.settle_s
            return
        if now >= c["settle_at"]:
            self._book_cycle(now)

    def _book_cycle(self, now: float) -> None:
        c = self.st["cycle"]
        free = sum(self.v.usdc(w) for w in WALLETS)
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
        usd = c["equity_start"] - free - sum(self.v.queued(w) for w in WALLETS)
        tot = self.st["totals"]
        tot["cycles"] += 1
        tot["usd_spent"] += usd
        tot["paper_minted"] += paper
        ea, eb = c["legs"]["A"]["entry"], c["legs"]["B"]["entry"]
        skewed = bool(ea and eb and abs(ea / eb - 1) > 0.002)  # legs filled far apart: not a normal cycle
        if not c["aborted"] and not skewed:
            self.st["history"].append({"ts": now, "usd": usd, "paper": paper})
        note = "(avorté) " if c["aborted"] else "(entrées décalées, hors plafond) " if skewed else ""
        log.info("Cycle #%d %sterminé en %.0f min: coût %.2f$, +%s PAPER (%.2f$/1000). Total: %s PAPER pour %.2f$",
                 c["n"], note, (now - c["started"]) / 60, usd, f"{paper:,.0f}",
                 cost_per_1000(usd, paper), f"{tot['paper_minted']:,.0f}", tot["usd_spent"])
        hit, avg = cost_cap_hit(self.st["history"], self.p)
        if hit:
            span = "le dernier cycle" if self.p.cost_window <= 1 else f"les {self.p.cost_window} derniers cycles"
            self.pause(f"coût {avg:.2f}$/1000 PAPER sur {span} > plafond {self.p.max_cost_per_1000:.2f}$: "
                       "farming arrêté")

    # --- staking --------------------------------------------------------
    def _track_stakes(self, now: float) -> None:
        pending = self.st["pending_stakes"]
        for w in list(pending):
            ps = pending[w]
            if self.v.paper_raw(w) <= ps["before"] - ps["raw"] + ps["raw"] // 1000:
                self.st["totals"]["paper_staked"] += ps["raw"] / SCALE
                log.info("Wallet %s: stake de %s PAPER confirmé", w, f"{ps['raw'] / SCALE:,.0f}")
                del pending[w]
                self.st["stake_failures"].pop(w, None)
            elif now > ps["until"]:
                log.warning("Wallet %s: stake jamais exécuté (ordre expiré), il sera renvoyé", w)
                del pending[w]
                self._stake_failed(w)

    def _stake_failed(self, w: str) -> None:
        n = self.st["stake_failures"][w] = self.st["stake_failures"].get(w, 0) + 1
        if n >= 3:
            log.error("Wallet %s: 3 stakes en échec, staking automatique désactivé pour ce wallet "
                      "(`python -m ptbot stake-now` ou le site). Le farming continue", w)

    def stake_if_needed(self, now: float, force: bool = False) -> None:
        """Stakes each wallet's whole unstaked PAPER once it reaches the threshold.
        Called between cycles (PAPER is minted when positions close); no pair is
        opened while a stake is unconfirmed, so it cannot skew a cycle's count."""
        if not force and now - self._last_stake_check < self.t.stake_every_s:
            return
        self._last_stake_check = now
        threshold = int(self.p.stake_threshold * SCALE)
        for w in WALLETS:
            if w in self.st["pending_stakes"] or self.st["stake_failures"].get(w, 0) >= 3:
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
                self._stake_failed(w)
                continue
            except Exception as e:
                log.warning("Stake sur %s: pas de réponse (%s), je vérifie on-chain", w, e)
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
        if r["step"] == "withdraw":
            if not self.v.deposit_address(dst):
                self._finish_rebalance(f"adresse de dépôt PaperTrade du wallet {dst} manquante "
                                       f"(DEPOSIT_ADDRESS_{dst} dans .env)")
                return
            r["withdraw_attempts"] += 1
            if r["withdraw_attempts"] > 3:
                self._finish_rebalance(f"le retrait de {amount:.2f}$ depuis {src} n'a jamais été exécuté (3 essais)")
                return
            r.update(spot_before=self.v.core_spot_usdc(src), perp_before=self.v.core_perp_usdc(src),
                     src_before=self.v.usdc(src))
            self._set_step(r, "await_core", now)
            self.store.save()  # write-ahead: a restart waits for this withdrawal instead of sending another
            try:
                self.v.withdraw_to_core(src, amount)
            except IntentRejected as e:
                self._finish_rebalance(f"retrait de {amount:.2f}$ depuis {src} refusé: {e}")
                return
            except Exception as e:
                log.warning("Rééquilibrage: retrait sans réponse (%s), je surveille HyperCore", e)
                return
            log.info("Rééquilibrage: retrait de %.2f$ de PaperTrade %s vers HyperCore", amount, src)
        elif r["step"] == "await_core":
            received = self.v.core_spot_usdc(src) - r["spot_before"]
            if _arrived(received, amount):
                r["send_amount"] = floor_cents(min(amount, received))
                self._set_step(r, "send", now)
                r["next_poll"] = 0
            elif not r.get("moved_from_perp") and _arrived(self.v.core_perp_usdc(src) - r["perp_before"], amount):
                log.info("Rééquilibrage: USDC arrivés en perp sur HyperCore, passage en spot")
                self.v.core_perp_to_spot(src, amount)
                r["moved_from_perp"] = True
            elif (now - r["step_started"] > WITHDRAW_DEADLINE_S + self.t.indexer_slack_s
                  and self.v.usdc(src) >= r["src_before"] - 0.5):
                log.warning("Rééquilibrage: le retrait a expiré sans être exécuté, nouvel essai")
                self._set_step(r, "withdraw", now)
            else:
                self._rebalance_wait(r, now, "l'arrivée des USDC sur HyperCore")
        elif r["step"] == "send":
            r["send_attempts"] += 1
            if r["send_attempts"] > 3:
                self._finish_rebalance(f"envoi HyperCore {src} -> dépôt {dst} en échec 3 fois: "
                                       f"{r['send_amount']:.2f}$ sont sur le spot HyperCore de {src}")
                return
            dest = self.v.deposit_address(dst)
            r.update(dst_before=self.v.usdc(dst), spot_before_send=self.v.core_spot_usdc(src))
            self._set_step(r, "await_credit", now)
            self.store.save()
            try:
                self.v.core_spot_send(src, dest, r["send_amount"])
            except Exception as e:
                if self.v.core_spot_usdc(src) > r["spot_before_send"] - r["send_amount"] / 2:
                    log.warning("Rééquilibrage: envoi vers %s échoué (%s), nouvel essai", dst, e)
                    self._set_step(r, "send", now)
                    r["next_poll"] = now + 30
                    return
            log.info("Rééquilibrage: %.2f$ envoyés vers l'adresse de dépôt de %s (%s)", r["send_amount"], dst, dest)
        elif r["step"] == "await_credit":
            if _arrived(self.v.usdc(dst) - r["dst_before"], r["send_amount"]):
                log.info("Rééquilibrage terminé: A=%.2f$, B=%.2f$", self.v.usdc("A"), self.v.usdc("B"))
                self._finish_rebalance(None)
            else:
                self._rebalance_wait(r, now, f"le crédit sur le compte PaperTrade de {dst}")

    def _rebalance_wait(self, r: dict, now: float, what: str) -> None:
        """No position is open while rebalancing, so waiting is safe: warn, and only give up late."""
        waited = now - r["step_started"]
        if waited >= self.t.rebalance_give_up_s:
            self._finish_rebalance(f"rééquilibrage: toujours en attente de {what} après {waited / 3600:.1f} h, "
                                   "vérifiez les soldes PaperTrade et HyperCore à la main")
        elif now - r["warned"] >= self.t.rebalance_warn_s:
            r["warned"] = now
            log.warning("Rééquilibrage: j'attends %s depuis %.0f min", what, waited / 60)

    def _finish_rebalance(self, error: str | None) -> None:
        self.st["rebalance"] = None
        self.st["phase"] = "idle"
        self.st["next_try"] = 0
        if error:
            self.pause(error)
