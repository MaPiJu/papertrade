"""HTTP client for exchange.papertrade.xyz (public reads + POST /intents)."""
from __future__ import annotations

import email.utils
import json
import logging
import time

import requests

from .constants import DEFAULT_MIN_MARGIN_USD, DEFAULT_MIN_NOTIONAL_USD, SCALE

log = logging.getLogger(__name__)

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "papertrade-delta-bot/1.0",
}


class IntentRejected(RuntimeError):
    """The relayer answered and refused the intent: it will never execute."""


LIST_KEYS = ("items", "events", "trades", "positions", "rows", "data")
TIME_KEYS = ("timestamp", "ts", "time", "timeMs", "createdAt", "blockTime", "blockTimestamp", "blockNumber", "block")


def _event_time(item) -> float | None:
    if not isinstance(item, dict):
        return None
    for source in [item] + [v for v in item.values() if isinstance(v, dict)]:
        for k in TIME_KEYS:
            v = source.get(k)
            if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
                return float(v)
    return None


def _page_order(page: list) -> str | None:
    """'asc' (oldest first) or 'desc' (newest first) from the events' timestamps."""
    times = [t for t in map(_event_time, page) if t is not None]
    if len(times) < 2 or times[0] == times[-1]:
        return None
    return "asc" if times[-1] > times[0] else "desc"


def _list_payload(data) -> list:
    """The list inside a read response. An unexpected shape raises instead of
    being read as "no positions", which would make the bot lose track of a pair."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in LIST_KEYS:
            if isinstance(data.get(key), list):
                return data[key]
    raise ValueError(f"réponse inattendue de l'API (clés {sorted(data) if isinstance(data, dict) else type(data)})")


class PaperTradeApi:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update(HEADERS)
        self._orders: dict[str, str] = {}
        self._pages: dict[str, dict] = {}

    def _request(self, method: str, path: str, timeout: float | None = None, retry: bool = True, **kw):
        # Only GETs are retried: a POSTed intent may already be queued even if the
        # response was lost, and resending it would be refused as a reused nonce.
        delays = (0.5, 1.5, 4.0) if method == "GET" and retry else ()
        for attempt in range(len(delays) + 1):
            try:
                r = self.http.request(method, self.base + path, timeout=timeout or self.timeout, **kw)
            except requests.RequestException:
                if attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            return r
        raise RuntimeError("unreachable")

    def get(self, path: str, params: dict | None = None, timeout: float | None = None, retry: bool = True):
        r = self._request("GET", path, timeout=timeout, retry=retry, params=params)
        r.raise_for_status()
        return r.json()

    # --- reads -------------------------------------------------------------
    def trading_state(self) -> dict:
        return self.get("/state/trading")

    def minimums(self) -> tuple[float, float]:
        """(minimum margin, minimum notional) in USD, from /state/trading."""
        try:
            st = self.trading_state()
            return (int(st["minimumTradeSizeRaw"]) / SCALE, int(st["minimumOpenNotionalRaw"]) / SCALE)
        except Exception as e:
            log.warning("Minimums illisibles (%s): valeurs par défaut %s$ / %s$",
                        e, DEFAULT_MIN_MARGIN_USD, DEFAULT_MIN_NOTIONAL_USD)
            return DEFAULT_MIN_MARGIN_USD, DEFAULT_MIN_NOTIONAL_USD

    def trading_paused(self) -> bool:
        try:
            return bool(self.trading_state().get("tradingPaused"))
        except Exception:
            return False

    def account_live(self, address: str, timeout: float = 8.0) -> dict:
        """First full snapshot of /state/user/live, the server-sent event stream the website
        reads: open positions, balances, PAPER. Later events are patches, so the stream is closed
        as soon as the snapshot is in. The trades history only lists closed positions."""
        deadline = time.time() + timeout
        r = self.http.get(self.base + "/state/user/live", params={"wallet": address.lower()}, stream=True,
                          timeout=(5.0, timeout), headers={"Accept": "text/event-stream"})
        try:
            r.raise_for_status()
            event, data = None, []
            # chunk_size=1: the default 512 would wait for more bytes than a small snapshot holds
            for raw in r.iter_lines(chunk_size=1):
                if time.time() > deadline:
                    break
                line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw or "").rstrip("\r")
                if not line:
                    if data and event in (None, "wallet"):
                        snap = json.loads("\n".join(data))
                        if (isinstance(snap, dict) and "baseAccountRevision" not in snap
                                and isinstance(snap.get("openPositions"), list)):
                            return snap
                    event, data = None, []
                elif not line.startswith(":"):
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        event = value
                    elif field == "data":
                        data.append(value)
        finally:
            r.close()
        raise RuntimeError(f"flux /state/user/live: pas d'instantané complet du compte en {timeout:.0f} s")

    def account_trades(self, address: str, pages: int = 3, timeout: float | None = None, retry: bool = True) -> list:
        """The account's trade events. A newest-first feed only needs its first pages; an
        oldest-first feed is followed to its end (its full pages never change, so they are
        cached and fetched once), otherwise new positions would vanish past page 3."""
        key = address.lower()
        cache = self._pages.setdefault(key, {})
        items, cursor, fetched = [], None, 0
        while True:
            if cursor in cache:
                page, nxt = cache[cursor]
            else:
                data = self.get(f"/query/accounts/{key}/trades",
                                params={"cursor": cursor} if cursor else None, timeout=timeout, retry=retry)
                page = _list_payload(data)
                nxt = data.get("nextCursor") if isinstance(data, dict) else None
                fetched += 1
                order = _page_order(page)
                if order:
                    self._orders[key] = order
                if nxt and self._orders.get(key) == "asc":
                    cache[cursor] = (page, nxt)
            items.extend(page)
            if not nxt:
                break
            if self._orders.get(key) != "asc" and fetched >= pages:
                break
            if fetched >= 200:
                log.warning("Historique de %s: plus de 200 pages lues, lecture tronquée", key)
                break
            cursor = nxt
        return items

    def trades_order(self, address: str) -> str | None:
        return self._orders.get(address.lower())

    def emission(self) -> float | None:
        """PAPER minted per $ (before the 2% deduction), as the website computes it from
        /query/protocol/summary: rate while the tracked LP is under the cliff, then
        rate x (decay / (decay + tailProgress))^2."""
        paper = self.get("/query/protocol/summary", timeout=10).get("paper") or {}
        tracked, cliff, rate, decay, tail = (int(paper[k]) / SCALE for k in
                                             ("trackedLp", "cliff", "rate", "decay", "tailProgress"))
        return rate if tracked < cliff else rate * (decay / (decay + tail)) ** 2

    def queue_total(self) -> float:
        """Total profit waiting in the protocol's FIFO queue (USD)."""
        data = self.get("/query/protocol/queue-summary")
        return int(data.get("total") or 0) / SCALE

    def server_time(self) -> float:
        r = self._request("GET", "/query/ready", timeout=10)
        return email.utils.parsedate_to_datetime(r.headers["Date"]).timestamp()

    def deposit_address(self, address: str) -> str | None:
        r = self._request("POST", "/funding/deposits/check", json={"user": address.lower()})
        if not r.ok:
            return None
        return (r.json() or {}).get("proxy")

    # --- writes ------------------------------------------------------------
    def cancel(self, intent_id: str, signed: dict) -> dict:
        r = self._request("POST", f"/intents/{intent_id}/cancel", json=signed)
        if 400 <= r.status_code < 500:
            raise IntentRejected(f"annulation de {intent_id} refusée (HTTP {r.status_code}): {r.text[:200]}")
        if not r.ok:
            raise RuntimeError(f"annulation de {intent_id}: réponse ambiguë (HTTP {r.status_code})")
        try:
            return r.json() or {}
        except ValueError:
            return {}

    def submit(self, body: dict) -> dict:
        """IntentRejected only when the relayer clearly refused (4xx or an error
        payload). A 5xx, a gateway error or an unreadable body may come after the
        intent was queued: that is raised as a plain error ("maybe executed")."""
        r = self._request("POST", "/intents", json=body)
        try:
            data = r.json()
        except ValueError:
            data = None
        if 400 <= r.status_code < 500:
            raise IntentRejected(f"{body['action']} refusé (HTTP {r.status_code}): {data or r.text[:300]}")
        if not r.ok or not isinstance(data, dict):
            raise RuntimeError(f"{body['action']}: réponse ambiguë (HTTP {r.status_code}), l'ordre a peut-être été pris")
        if data.get("error") or data.get("ok") is False:
            raise IntentRejected(f"{body['action']} refusé: {data}")
        return data
