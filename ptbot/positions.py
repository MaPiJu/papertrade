"""Rebuilds a wallet's open positions from the PaperTrade trade history.

The read API is undocumented, so the field names below are a tolerant guess
covering the usual spellings. `python -m ptbot probe` dumps the raw responses:
if open positions come out empty while the site shows some, adjust the key
lists here from that output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .constants import INSTRUMENTS, SCALE

ID_KEYS = ("positionId", "position_id", "posId", "positionID")
KIND_KEYS = ("kind", "type", "event", "eventType", "action", "status", "reason")
CLOSED_MARKERS = ("closedAt", "closeTime", "closedTime", "exitPrice", "closePrice", "closeTs")
COIN_KEYS = ("instrumentId", "instrument", "marketId", "market", "coin", "asset", "symbol")
SIDE_BOOL_KEYS = ("isLong", "long")
SIDE_STR_KEYS = ("side", "direction", "positionSide")
MARGIN_KEYS = ("margin", "marginRaw", "size", "sizeRaw", "collateral")
LEVERAGE_KEYS = ("leverage", "lev")
ENTRY_KEYS = ("entryPrice", "entryPx", "openPrice", "entry")

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
    s = _first(rec, SIDE_STR_KEYS)
    if isinstance(s, str):
        s = s.lower()
        if s in ("long", "buy", "l", "b"):
            return True
        if s in ("short", "sell", "s", "a"):
            return False
    return None


def _kind(rec: dict) -> str:
    text = " ".join(str(rec[k]).lower() for k in KIND_KEYS if isinstance(rec.get(k), str))
    if "liquid" in text or any(rec.get(k) not in (None, "", 0, "0") for k in CLOSED_MARKERS):
        return "closed"
    if any(w in text for w in ("close", "exit", "settle", "cancel")):
        return "closed"
    if "open" in text:
        return "open"
    return "unknown"


def open_positions(payload: Any) -> list[Position]:
    """Positions with an open event and no close/liquidation event."""
    opened: dict[int, Position] = {}
    closed: set[int] = set()
    for rec in _records(payload):
        pid = _first(rec, ID_KEYS)
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        kind = _kind(rec)
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
    return [p for pid, p in opened.items() if pid not in closed]


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
