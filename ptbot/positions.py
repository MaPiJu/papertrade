"""A wallet's positions from the PaperTrade read API.

Open positions come from the live account stream (`live_positions`), as on the website. The
trade history (`open_positions`) only lists closed positions (kind "PositionClosed"); it is
kept for diagnostics. `python -m ptbot probe` dumps the raw responses.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .constants import INSTRUMENTS, SCALE

ID_KEYS = ("positionId", "position_id", "posId", "positionID")
KIND_KEYS = ("kind", "type", "event", "eventType", "action", "status", "reason", "dir")
CLOSED_MARKERS = ("closedAt", "closeTime", "closedTime", "exitPrice", "closePrice", "closeTs")
COIN_KEYS = ("instrumentId", "instrument", "marketId", "market", "coin", "asset", "symbol")
SIDE_BOOL_KEYS = ("isLong", "long")
SIDE_STR_KEYS = ("side", "direction", "positionSide")
MARGIN_KEYS = ("margin", "marginRaw", "size", "sizeRaw", "collateral")
LEVERAGE_KEYS = ("leverage", "lev")
ENTRY_KEYS = ("entryPrice", "entryPriceRaw", "entryPx", "openPrice", "entry")

ID_TO_COIN = {v: k for k, v in INSTRUMENTS.items()}


@dataclass
class Position:
    id: int
    coin: str | None
    is_long: bool | None
    margin: float | None = None
    leverage: int | None = None
    entry: float | None = None


def _records(payload: Any) -> Iterable[dict]:
    if isinstance(payload, dict):
        for key in ("items", "events", "trades", "positions", "rows", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        flat = dict(item)
        for v in item.values():          # one level of nesting, e.g. {"position": {...}}
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    flat.setdefault(k2, v2)
        yield flat


def _first(rec: dict, keys: tuple) -> Any:
    for k in keys:
        if rec.get(k) not in (None, ""):
            return rec[k]
    return None


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _usd(v: Any) -> float | None:
    x = _num(v)
    if x is None:
        return None
    return x / SCALE if abs(x) > 1e12 else x


def _coin(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        return ID_TO_COIN.get(int(v))
    s = str(v).upper()
    return next((c for c in INSTRUMENTS if c in s), None)


def _side(rec: dict) -> bool | None:
    b = _first(rec, SIDE_BOOL_KEYS)
    if isinstance(b, bool):
        return b
    if isinstance(b, str) and b.lower() in ("true", "false"):
        return b.lower() == "true"
    for key in SIDE_STR_KEYS + ("dir",):
        s = rec.get(key)
        if not isinstance(s, str):
            continue
        s = s.lower()
        if s in ("long", "buy", "l", "b") or "long" in s:
            return True
        if s in ("short", "sell", "s", "a") or "short" in s:
            return False
    return None


def _present(v: Any) -> bool:
    return v not in (None, "") and _num(v) != 0


def _kind(rec: dict) -> str:
    text = " ".join(str(rec[k]).lower() for k in KIND_KEYS if isinstance(rec.get(k), str))
    if any(_present(rec.get(k)) for k in CLOSED_MARKERS):
        return "closed"
    if any(w in text for w in ("liquid", "bust", "adl", "deleverag", "close", "exit", "settle", "cancel")):
        return "closed"
    if "open" in text:
        return "open"
    return "unknown"


def open_positions(payload: Any) -> list[Position]:
    """Positions with an open event and no close/liquidation event. Fails closed: events
    that the parser cannot read raise instead of reading as "no position"."""
    opened: dict[int, Position] = {}
    closed: set[int] = set()
    records = list(_records(payload))
    with_id = known_kind = 0
    for rec in records:
        pid = _first(rec, ID_KEYS)
        try:
            pid = int(pid, 16) if isinstance(pid, str) and pid.lower().startswith("0x") else int(pid)
        except (TypeError, ValueError):
            continue
        with_id += 1
        kind = _kind(rec)
        known_kind += kind != "unknown"
        if kind == "closed":
            closed.add(pid)
        elif kind == "open":
            lev = _num(_first(rec, LEVERAGE_KEYS))
            opened[pid] = Position(
                id=pid,
                coin=_coin(_first(rec, COIN_KEYS)),
                is_long=_side(rec),
                margin=_usd(_first(rec, MARGIN_KEYS)),
                leverage=int(lev) if lev else None,
                entry=_num(_first(rec, ENTRY_KEYS)),
            )
    if records and (not with_id or not known_kind):
        raise ValueError(f"format des événements non reconnu ({len(records)} événements, {with_id} avec un id de "
                         f"position, {known_kind} classés): lancez `python -m ptbot probe` et adaptez positions.py")
    return [p for pid, p in opened.items() if pid not in closed]


def _flag(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str) and v.lower() in ("true", "false", "1", "0"):
        return v.lower() in ("true", "1")
    return None


def live_positions(snapshot: Any) -> list[Position]:
    """Open positions of a /state/user/live snapshot. Rows are [positionId, instrumentId, isLong,
    useDebt, entryPriceRaw, marginRaw, leverage, bustPriceRaw, openedAt] (objects also read).
    Anything unreadable raises: an empty list must mean "no open position"."""
    rows = snapshot.get("openPositions") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        raise ValueError("instantané du compte sans liste openPositions")
    out = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 7:
            pid, inst, side, _debt, entry, margin, lev = row[:7]
            is_long = _flag(side)
        elif isinstance(row, dict):
            pid, inst, entry = _first(row, ID_KEYS + ("id",)), _first(row, COIN_KEYS), _first(row, ENTRY_KEYS)
            margin, lev, is_long = _first(row, MARGIN_KEYS), _first(row, LEVERAGE_KEYS), _side(row)
        else:
            raise ValueError(f"position illisible dans l'instantané du compte: {row!r}"[:200])
        try:
            pid = int(pid, 16) if isinstance(pid, str) and pid.lower().startswith("0x") else int(pid)
        except (TypeError, ValueError):
            raise ValueError(f"id de position illisible: {row!r}"[:200]) from None
        lev = _num(lev)
        out.append(Position(id=pid, coin=_coin(inst), is_long=is_long, margin=_usd(margin),
                            leverage=int(lev) if lev else None, entry=_num(entry)))
    return out


def plausible_price(raw: float | None, reference: float, tolerance: float = 0.03) -> float | None:
    """Converts a raw on-chain price to USD by trying the usual scales, keeping the
    one within `tolerance` of the reference mid. None if nothing matches."""
    if not raw or reference <= 0:
        return None
    for scale in (1, 10, 100, 1e4, 1e6, 1e8, 1e18):
        px = raw / scale
        if abs(px / reference - 1) <= tolerance:
            return px
    return None
