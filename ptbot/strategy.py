"""Pure decision logic: sizing, leverage, exit distance, rebalancing, cost cap.

Cycle (rusk0f's method):
  1. wallet A opens a long and wallet B a short: same coin, margin and leverage;
  2. once price has moved `close_move` from entry, BOTH legs are closed BEFORE
     the losing leg reaches its liquidation price;
  3. the losing leg's realised loss mints PAPER; the winning leg brings back
     most of the move (minus PaperTrade's haircut on profits);
  4. USDC is rebalanced between A and B, PAPER is staked, and the next cycle starts.
Farming stops once a cycle costs more than `max_cost_per_1000` USD per 1,000 PAPER.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import LIQ_BUFFER


@dataclass
class Params:
    coin: str = "BTC"
    leverage: int = 100
    max_leverage: int = 100            # > leverage lets the bot go up to reach the minimum notional
    close_move: float = 0.007          # close both legs once price moved 0.70% from entry
    liq_safety: float = 0.0015         # always close at least 0.15% before the liquidation price
    margin_fraction: float = 1.0       # share of the smaller balance used as margin per leg
    max_margin_usd: float = 0.0        # 0 = no cap
    rebalance_threshold: float = 0.10  # rebalance when |A-B| / (A+B) exceeds this
    min_rebalance_usd: float = 5.0
    stake_threshold: float = 1000.0    # stake as soon as a wallet holds this much unstaked PAPER
    max_cost_per_1000: float = 7.0     # USD per 1,000 PAPER above which farming stops
    cost_window: int = 1               # cycles averaged for the cost cap (1 = rusk0f's per-cycle rule)


@dataclass
class OpenPlan:
    margin: float
    leverage: int
    close_move: float

    @property
    def notional(self) -> float:
        return self.margin * self.leverage


@dataclass
class Skip:
    reason: str


@dataclass
class Transfer:
    src: str
    dst: str
    amount: float


def liq_distance(leverage: int) -> float:
    """Adverse move that liquidates a position (docs: ~5 bps before zero equity)."""
    return 1.0 / leverage - LIQ_BUFFER


def exit_distance(leverage: int, p: Params) -> float:
    return min(p.close_move, liq_distance(leverage) - p.liq_safety)


def floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


def plan_open(bal_a: float, bal_b: float, min_margin: float, min_notional: float, p: Params) -> OpenPlan | Skip:
    margin = floor_cents(min(bal_a, bal_b) * p.margin_fraction)
    if p.max_margin_usd > 0:
        margin = min(margin, p.max_margin_usd)
    if margin < min_margin:
        return Skip(f"marge {margin:.2f}$ < minimum {min_margin:.2f}$ par position")
    leverage = max(p.leverage, math.ceil(min_notional / margin - 1e-9))
    if leverage > p.max_leverage:
        return Skip(f"notionnel {margin * p.max_leverage:,.0f}$ < minimum {min_notional:,.0f}$ "
                    f"(marge {margin:.2f}$ x{p.max_leverage}): rechargez les wallets ou montez MAX_LEVERAGE "
                    f"(il faudrait x{leverage})")
    move = exit_distance(leverage, p)
    if move <= 0:
        return Skip(f"x{leverage}: liquidation trop proche ({liq_distance(leverage):.3%}) pour LIQ_SAFETY")
    return OpenPlan(margin=margin, leverage=leverage, close_move=move)


def plan_rebalance(bal_a: float, bal_b: float, p: Params, min_notional: float = 0.0) -> Transfer | None:
    total = bal_a + bal_b
    diff = abs(bal_a - bal_b)
    if total <= 0:
        return None
    amount = floor_cents(diff / 2)
    # Even a small gap must be closed when it alone keeps the smaller wallet under the minimum notional.
    blocking = min(bal_a, bal_b) * p.max_leverage < min_notional <= total / 2 * p.max_leverage
    if amount <= 0 or (not blocking and (diff / total < p.rebalance_threshold or amount < p.min_rebalance_usd)):
        return None
    return Transfer("A", "B", amount) if bal_a > bal_b else Transfer("B", "A", amount)


def price_move(entry: float, mid: float) -> float:
    return mid / entry - 1.0


def should_exit(entry: float, mid: float, close_move: float) -> bool:
    return abs(price_move(entry, mid)) >= close_move - 1e-12


def cost_per_1000(usd_spent: float, paper_minted: float) -> float:
    return math.inf if paper_minted <= 0 else usd_spent / paper_minted * 1000


def cost_cap_hit(history: list[dict], p: Params) -> tuple[bool, float]:
    """Average cost of the last `cost_window` cycles against the cap (judged only
    once that many cycles exist, so one unlucky cycle doesn't stop the farm)."""
    window = max(p.cost_window, 1)
    recent = history[-window:]
    if not recent:
        return False, 0.0
    avg = cost_per_1000(sum(c["usd"] for c in recent), sum(c["paper"] for c in recent))
    return len(recent) >= window and avg > p.max_cost_per_1000, avg
