"""Offline PaperTrade simulator, same interface as LiveVenue.

Mechanics modelled from the docs and community measurements (estimates):
  - open/close at the BBO mid, no fees, no funding;
  - loss: PAPER = (loss - 2%) x emission; liquidation: PAPER = margin x emission;
  - profit: haircut h(m) = 1 - (1-b)·m'/(m'+K), m' = move - 0.2 bps.
    b=0.10, K=0.000216 reproduce ~48% kept at 0.03%, ~74% at 0.1%, ~88% at 1%.
"""
from __future__ import annotations

import itertools
import math
import random
import time
from dataclasses import dataclass, field

from .constants import SCALE
from .papertrade_api import IntentRejected
from .positions import Position
from .strategy import liq_distance

DEADBAND = 0.00002

# (b, K) hypotheses for the profit haircut, from most to least favourable:
# community measurements after launch, rusk0f's reported costs, pre-launch bytecode reading.
HAIRCUT_MODELS = {
    "optimiste": (0.10, 0.000216),
    "rusk0f": (0.10, 0.001868),
    "pessimiste": (0.10, 0.0101),
}


def haircut(move: float, b: float, k: float) -> float:
    m = move - DEADBAND
    if m <= 0:
        return 1.0
    return 1 - (1 - b) * m / (m + k)


@dataclass
class SimPos:
    id: int
    coin: str
    is_long: bool
    margin: float
    leverage: int
    entry: float


@dataclass
class SimAccount:
    usdc: float
    paper_raw: int = 0
    staked_raw: int = 0
    core_spot: float = 0.0
    positions: dict = field(default_factory=dict)


class RandomWalk:
    """Synthetic BBO mid: Gaussian log-returns, `vol_per_hour` scaled per second."""

    def __init__(self, start: float = 100_000.0, vol_per_hour: float = 0.006, seed: int = 1) -> None:
        self.px = start
        self.t = None
        self.sigma_s = vol_per_hour / math.sqrt(3600)
        self.rng = random.Random(seed)

    def __call__(self, now: float) -> float:
        if self.t is None:
            self.t = now
        steps = int(now - self.t)
        for _ in range(steps):
            self.px *= math.exp(self.rng.gauss(0, self.sigma_s))
        self.t += steps
        return self.px


class SimVenue:
    def __init__(self, price_fn, start_usdc: float = 100.0, emission: float = 50.0,
                 haircut_b: float = 0.10, haircut_k: float = 0.000216, loss_fee: float = 0.02,
                 min_margin: float = 10.0, min_notional: float = 10_000.0, realtime: bool = False) -> None:
        self.price_fn = price_fn
        self.emission = emission
        self.b, self.k = haircut_b, haircut_k
        self.loss_fee = loss_fee
        self.mins = (min_margin, min_notional)
        self.realtime = realtime
        self.clock = time.time() if realtime else 1_800_000_000.0
        self.accounts = {"A": SimAccount(start_usdc), "B": SimAccount(start_usdc)}
        self.ids = itertools.count(1)
        self.log: list[str] = []
        self.liquidations = 0

    # --- clock -----------------------------------------------------------
    def now(self) -> float:
        return time.time() if self.realtime else self.clock

    def sleep(self, seconds: float) -> None:
        if self.realtime:
            time.sleep(seconds)
        else:
            self.clock += seconds

    # --- market ----------------------------------------------------------
    def mid(self, coin: str) -> float:
        return self.price_fn(self.now())

    def _liquidate(self) -> None:
        for acc in self.accounts.values():
            for pid, p in list(acc.positions.items()):
                px = self.mid(p.coin)
                adverse = (p.entry - px) / p.entry if p.is_long else (px - p.entry) / p.entry
                if adverse >= liq_distance(p.leverage):
                    del acc.positions[pid]
                    acc.paper_raw += int(p.margin * self.emission * SCALE)
                    self.liquidations += 1
                    self.log.append(f"liquidation #{pid}")

    # --- reads -----------------------------------------------------------
    def usdc(self, w: str) -> float:
        self._liquidate()
        return self.accounts[w].usdc

    def queued(self, w: str) -> float:
        return 0.0

    def paper_raw(self, w: str) -> int:
        self._liquidate()
        return self.accounts[w].paper_raw

    def positions(self, w: str) -> list[Position]:
        self._liquidate()
        return [Position(p.id, p.coin, p.is_long, p.margin, p.leverage, p.entry)
                for p in self.accounts[w].positions.values()]

    def minimums(self) -> tuple[float, float]:
        return self.mins

    def trading_paused(self) -> bool:
        return False

    def core_spot_usdc(self, w: str) -> float:
        return self.accounts[w].core_spot

    def core_perp_usdc(self, w: str) -> float:
        return 0.0

    def deposit_address(self, w: str) -> str:
        return f"sim-deposit-{w}"

    # --- writes ----------------------------------------------------------
    def open(self, w: str, coin: str, is_long: bool, margin: float, leverage: int, deadline_s: int) -> str:
        self._liquidate()
        acc = self.accounts[w]
        if margin > acc.usdc + 1e-9:
            raise IntentRejected(f"solde insuffisant sur {w}")
        if margin < self.mins[0] or margin * leverage < self.mins[1] - 1e-6:
            raise IntentRejected("taille sous le minimum")
        pid = next(self.ids)
        acc.usdc -= margin
        acc.positions[pid] = SimPos(pid, coin, is_long, margin, leverage, self.mid(coin))
        return f"sim-open-{pid}"

    def close(self, w: str, position_ids: list[int]) -> str:
        self._liquidate()
        acc = self.accounts[w]
        for pid in position_ids:
            p = acc.positions.pop(pid, None)
            if p is None:
                continue
            px = self.mid(p.coin)
            move = (px - p.entry) / p.entry * (1 if p.is_long else -1)
            pnl = p.margin * p.leverage * move
            if pnl < 0:
                loss = min(-pnl, p.margin)
                acc.usdc += p.margin - loss
                acc.paper_raw += int(loss * (1 - self.loss_fee) * self.emission * SCALE)
            else:
                acc.usdc += p.margin + pnl * (1 - haircut(move, self.b, self.k))
        return "sim-close"

    def stake(self, w: str, amount_raw: int) -> str:
        acc = self.accounts[w]
        amount_raw = min(amount_raw, acc.paper_raw)
        acc.paper_raw -= amount_raw
        acc.staked_raw += amount_raw
        return "sim-stake"

    def withdraw_to_core(self, w: str, amount: float) -> str:
        acc = self.accounts[w]
        if amount > acc.usdc + 1e-9:
            raise IntentRejected("retrait supérieur au solde")
        acc.usdc -= amount
        acc.core_spot += amount
        return "sim-withdraw"

    def core_perp_to_spot(self, w: str, amount: float) -> None:
        raise RuntimeError("pas de perp en simulation")

    def core_spot_send(self, w: str, destination: str, amount: float) -> None:
        src = self.accounts[w]
        if amount > src.core_spot + 1e-9:
            raise RuntimeError("spot insuffisant")
        src.core_spot -= amount
        self.accounts[destination.rsplit("-", 1)[1]].usdc += amount
