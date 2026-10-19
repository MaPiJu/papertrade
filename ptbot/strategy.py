"""Pure decision logic: sizing, leverage, exit distance, rebalancing, cost cap.

Cycle (rusk0f's method):
  1. wallet A opens a long and wallet B a short: same coin, margin and leverage;
  2. once price has moved `close_move` from entry, BOTH legs are closed BEFORE
     the losing leg reaches its liquidation price;
  3. the losing leg's realised loss mints PAPER; the winning leg brings back
     most of the move (minus PaperTrade's haircut on profits);
  4. USDC is rebalanced between A and B, PAPER is staked, and the next cycle starts.
Farming stops once the USDC left is at or under `min_balance_usd`, or once a cycle costs more
than `max_cost_per_1000` USD per 1,000 PAPER (each rule off at 0).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .constants import LIQ_BUFFER


@dataclass
class Params:
    coin: str = "BTC"
    leverage: int = 250
    max_leverage: int = 250            # > leverage lets the bot go up to reach the minimum notional
    target_notional: float = 11_000.0  # margin per leg = what this notional needs at `leverage` (0 = whole balance)
    close_move: float = 0.007          # close both legs once price moved 0.70% from entry
    liq_safety: float = 0.0015         # always close at least 0.15% before the liquidation price
    margin_fraction: float = 1.0       # share of the smaller balance used as margin per leg
    max_margin_usd: float = 0.0        # 0 = no cap
    rebalance_threshold: float = 0.10  # rebalance when |A-B| / (A+B) exceeds this
    min_rebalance_usd: float = 11.0    # never below MIN_CORE_TRANSFER_USD
    stake_threshold: float = 1000.0    # stake as soon as a wallet holds this much unstaked PAPER
    max_cost_per_1000: float = 7.0     # USD per 1,000 PAPER above which farming stops (0 = no cost cap)
    cost_window: int = 1               # cycles averaged for the cost cap (1 = rusk0f's per-cycle rule)
    min_balance_usd: float = 0.0       # farming stops once the USDC left is at or under this (0 = never)
    strategy: str = "ladder"           # "ladder" (re-arm the losing leg) or "pair" (close both together)
    ladder_step: float = 0.002         # ladder: the losing leg is closed and re-opened after this move
    ladder_liq_buffer: float = 0.0025  # ladder legs: liquidation this far beyond the step (sets their leverage)
    ladder_reset: float = 0.0          # ladder: winner's gain that ends the period (0 = from emission and cap)
    ladder_cost_margin: float = 0.02   # ladder auto reset: aim this far under the cost cap
    ladder_max_leverage: int = 1000    # ladder legs: leverage set by the step (LEVERAGE is for "pair")
    one_wallet: bool = False           # both legs on wallet A (no transfers between A and B)


@dataclass
class OpenPlan:
    margin: float
    leverage: int
    close_move: float          # pair: exit of both legs; ladder: step of the losing leg
    reset: float = 0.0         # ladder: winner's gain that ends the period

    @property
    def notional(self) -> float:
        return self.margin * self.leverage


@dataclass
class Skip:
    reason: str
    capital: bool = False   # only the capital is short (another plan may still fit)


@dataclass
class Transfer:
    src: str
    dst: str
    amount: float


# Settlement of a close, checked to the cent against real trades (14/10/2026): the loser loses
# notional x move, PAPER is minted on 98% of that loss; the winner's gain goes through the
# "asymmetric impact" of the docs (BTC/ETH launch parameters) and then pays a 2% fee.
DEADBAND = 0.00002
WIN_FEE = 0.02
MINT_FACTOR = 0.98
IMPACT = {"BTC": (0.10, 15000.0, 100_000.0, 814.598), "ETH": (0.10, 15000.0, 100_000.0, 483.979)}


def win_payout(move: float, coin: str = "BTC") -> float:
    """$ received per $ of notional when a winning position is closed after `move`."""
    base, rate_mult, ref_notional, pos_mult = IMPACT.get(coin, IMPACT["BTC"])
    m = move - DEADBAND
    if m <= 0:
        return 0.0
    scale = (1 - base) / (1 + 1 / (m * rate_mult) + ref_notional / (1e6 * m * pos_mult))
    return m * scale * (1 - WIN_FEE)


def cost_per_1000_at(move: float, emission: float, coin: str = "BTC") -> float:
    """$ per 1000 PAPER when the loser loses `move` and the winner is realised at `move`."""
    if move <= 0 or emission <= 0:
        return math.inf
    return (move - win_payout(move, coin)) / (move * MINT_FACTOR * emission) * 1000


def liq_distance(leverage: int) -> float:
    """Adverse move that liquidates a position (docs: ~5 bps before zero equity)."""
    return 1.0 / leverage - LIQ_BUFFER


def exit_distance(leverage: int, p: Params) -> float:
    return min(p.close_move, liq_distance(leverage) - p.liq_safety)


def floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


def ceil_cents(x: float) -> float:
    return math.ceil(x * 100 - 1e-9) / 100


def plan_open(bal_a: float, bal_b: float, min_margin: float, min_notional: float, p: Params) -> OpenPlan | Skip:
    margin = floor_cents(min(bal_a, bal_b) * p.margin_fraction)
    if p.target_notional > 0:
        margin = min(margin, ceil_cents(p.target_notional / p.leverage))
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


MAX_LADDER_RESET = 0.06
# Without a cost cap, the move the automatic reset chose at launch (emission 18.9, cap 7$): a larger
# one costs a little less per PAPER but makes the periods, and the farming, much slower.
NO_CAP_LADDER_RESET = 0.02


def ladder_reset_move(emission: float | None, p: Params) -> float | None:
    """Winner's gain that ends a ladder period: fixed, 2% without a cost cap, or the smallest move
    whose cost per 1000 PAPER stays under the cap with a margin (the cost falls as the realised
    move grows)."""
    if p.ladder_reset > 0:
        return p.ladder_reset
    if p.max_cost_per_1000 <= 0:
        return NO_CAP_LADDER_RESET
    if not emission or emission <= 0:
        return None
    target = p.max_cost_per_1000 * (1 - p.ladder_cost_margin)
    move = max(4 * p.ladder_step, 0.005)
    while move <= MAX_LADDER_RESET + 1e-12:
        if cost_per_1000_at(move, emission, p.coin) <= target:
            return round(move, 6)
        move += 0.0005
    return None


def plan_ladder(free: dict[str, float], min_margin: float, min_notional: float, emission: float | None,
                p: Params) -> OpenPlan | Skip:
    """Ladder sizing. Each leg is closed after `ladder_step` against it and re-opened at once, so
    the losing side pays up to (reset + step) x notional during a period: the notional is sized so
    that it always can (in two-wallet mode each wallet must carry a one-sided period on its own; in
    one-wallet mode wallet A carries both legs)."""
    reset = ladder_reset_move(emission, p)
    if reset is None:
        what = f"émission {emission:.1f} PAPER/$" if emission else "émission illisible"
        return Skip(f"{what}: même en laissant le gagnant courir jusqu'à {MAX_LADDER_RESET:.0%}, le coût "
                    f"dépasserait {p.max_cost_per_1000:.2f}$/1000 PAPER")
    cost = cost_per_1000_at(reset, emission, p.coin) if emission else None
    if p.ladder_reset > 0 and p.max_cost_per_1000 > 0 and cost is not None and cost > p.max_cost_per_1000:
        return Skip(f"encaissement à +{reset:.2%} (LADDER_RESET_PCT): coût prévu {cost:.2f}$/1000 PAPER à l'émission "
                    f"actuelle ({emission:.2f}) > plafond {p.max_cost_per_1000:.2f}$ (MAX_COST_PER_1000_PAPER)")
    step = p.ladder_step
    # The old leg of a re-arm is closed ~5-10 s after the step is seen: the buffer must absorb that
    # window's move even when volatility spikes (0.25% keeps liquidations rare in simulations).
    max_lev = min(p.ladder_max_leverage, int(1 / (step + LIQ_BUFFER + p.ladder_liq_buffer)))
    if max_lev < 1:
        return Skip(f"pas de {step:.2%}: liquidation trop proche pour LADDER_LIQ_BUFFER_PCT")
    # Margins locked at once: each leg, plus a re-armed leg's new one before its old one comes back.
    legs_on_pool = 3 if p.one_wallet else 2
    pool = (free.get("A", 0.0) if p.one_wallet else min(free.values())) * p.margin_fraction
    span = reset + step
    # A re-opened leg keeps the other leg's BTC quantity, so its margin shrinks with the price by up
    # to `span` during a period: start far enough above the protocol's minimums.
    floor_margin = math.ceil(min_margin / (1 - span) * 100) / 100
    floor_notional = min_notional / (1 - span)
    leverage = max_lev
    margin = pool / (legs_on_pool + span * leverage)
    if p.max_margin_usd > 0:
        margin = min(margin, p.max_margin_usd)
    if margin < floor_margin:
        # Not enough capital at this leverage: keep the minimum margin and lower the leverage.
        margin = floor_margin
        leverage = min(max_lev, int((pool / floor_margin - legs_on_pool) / span))
    margin = max(floor_cents(margin), floor_margin)
    if leverage < 1 or margin * leverage < floor_notional:
        return Skip(f"capital trop faible pour l'échelle: {pool:.2f}$ disponibles pour couvrir "
                    f"{span:.2%} de pertes d'un côté (minimums {min_margin:.0f}$ de marge, {min_notional:,.0f}$ "
                    "de notionnel)", capital=True)
    return OpenPlan(margin=margin, leverage=leverage, close_move=step, reset=reset)


def choose_plan(bal: dict[str, float], min_margin: float, min_notional: float, emission: float | None,
                p: Params) -> tuple[OpenPlan | Skip, str | None]:
    """The next cycle and, when the ladder had to give way to a pair, why. A pair whose exit would
    cost more than the cap at the current emission is refused: that is the cap's own rule, applied
    before paying rather than after."""
    note = None
    pair_p = p
    if p.strategy == "ladder":
        plan = plan_ladder(bal, min_margin, min_notional, emission, p)
        if not (isinstance(plan, Skip) and plan.capital):
            return plan, None
        # A pair closed at the ladder's reset move costs what a period would.
        note = f"{plan.reason}: paire classique à la place"
        reset = ladder_reset_move(emission, p)
        lev = max(1, int(1 / (reset + LIQ_BUFFER + p.liq_safety)))
        pair_p = replace(p, close_move=reset, leverage=min(p.leverage, lev), max_leverage=lev)
    pair_bal = (bal["A"] / 2, bal["A"] / 2) if p.one_wallet else (bal["A"], bal["B"])
    plan = plan_open(*pair_bal, min_margin, min_notional, pair_p)
    if isinstance(plan, Skip) and pair_p is not p:
        plan = Skip(f"paire de repli limitée à x{pair_p.leverage} pour fermer à ±{pair_p.close_move:.2%} (coût d'une "
                    f"période): {plan.reason.split(':')[0]}; rechargez le wallet")
    if isinstance(plan, OpenPlan) and emission and p.max_cost_per_1000 > 0:
        cost = cost_per_1000_at(plan.close_move, emission, p.coin)
        if cost > p.max_cost_per_1000:
            plan = Skip(f"paire x{plan.leverage} fermée à ±{plan.close_move:.2%}: coût prévu {cost:.2f}$/1000 PAPER "
                        f"à l'émission actuelle ({emission:.2f}) > plafond {p.max_cost_per_1000:.2f}$")
    return plan, note


# PaperTrade withdrawals and deposits through HyperCore need 10 USDC, and a wallet's first
# withdrawal also pays 1 USDC to activate its HyperCore account (seen on the website 14/10/2026).
MIN_DEPOSIT_USD = 10.0
MIN_CORE_TRANSFER_USD = 11.0


def plan_rebalance(bal_a: float, bal_b: float, p: Params, mins: tuple[float, float] = (0.0, 0.0)) -> Transfer | None:
    """Half the gap, when the gap is large, or when it alone prevents opening a pair
    that equal balances would allow; never under the 11 USDC a transfer needs."""
    total = bal_a + bal_b
    diff = abs(bal_a - bal_b)
    amount = float(math.floor(diff / 2 + 1e-9))   # whole USDC, as the website withdraws
    if total <= 0 or amount <= 0:
        return None
    blocking = (mins[1] > 0 and isinstance(plan_open(bal_a, bal_b, *mins, p), Skip)
                and not isinstance(plan_open(total / 2, total / 2, *mins, p), Skip))
    if amount < max(MIN_CORE_TRANSFER_USD, 0 if blocking else p.min_rebalance_usd):
        return None
    if not blocking and diff / total < p.rebalance_threshold:
        return None
    return Transfer("A", "B", amount) if bal_a > bal_b else Transfer("B", "A", amount)


def price_move(entry: float, mid: float) -> float:
    return mid / entry - 1.0


def should_exit(entry: float, mid: float, close_move: float) -> bool:
    return abs(price_move(entry, mid)) >= close_move - 1e-12


def cost_per_1000(usd_spent: float, paper_minted: float) -> float:
    if usd_spent <= 0:
        return 0.0  # nothing spent (a cycle that broke even or made money)
    return math.inf if paper_minted <= 0 else usd_spent / paper_minted * 1000


# A ladder period's cost is set by its reset move and the emission, but fill timing adds noise
# around it (the re-opened leg and the old one fill seconds apart, ~1$/1000 per period). So its
# realised cost is judged over many periods, and only a clear excess over the cap pauses the farm
# (the expected cost is already held under the cap when each period is planned).
LADDER_COST_WINDOW = 20
LADDER_COST_Z = 2.5


def cost_cap_hit(history: list[dict], p: Params) -> tuple[bool, float]:
    """Average cost of the last `cost_window` cycles against the cap (judged only
    once that many cycles exist, so one unlucky cycle doesn't stop the farm)."""
    if history and history[-1].get("mode") == "ladder":
        return ladder_cost_hit([c for c in history if c.get("mode") == "ladder"], p)
    history = [c for c in history if c.get("mode") != "ladder"]   # each kind against its own rule
    window = max(p.cost_window, 1)
    recent = history[-window:]
    if not recent:
        return False, 0.0
    avg = cost_per_1000(sum(c["usd"] for c in recent), sum(c["paper"] for c in recent))
    return len(recent) >= window and avg > p.max_cost_per_1000, avg


def ladder_cost_hit(periods: list[dict], p: Params) -> tuple[bool, float]:
    """True when the last periods' average cost is above the cap by more than their own
    scatter explains (LADDER_COST_Z standard errors)."""
    recent = periods[-max(LADDER_COST_WINDOW, p.cost_window):]
    if not recent:
        return False, 0.0
    avg = cost_per_1000(sum(c["usd"] for c in recent), sum(c["paper"] for c in recent))
    if len(recent) < 3:
        return False, avg
    costs = [max(-50.0, min(50.0, cost_per_1000(c["usd"], c["paper"]) if c["usd"] > 0
                            else c["usd"] / max(c["paper"], 1) * 1000)) for c in recent]
    mean = sum(costs) / len(costs)
    se = math.sqrt(sum((x - mean) ** 2 for x in costs) / (len(costs) - 1) / len(costs))
    return avg - LADDER_COST_Z * se > p.max_cost_per_1000, avg
