"""HTTP client for exchange.papertrade.xyz (public reads + POST /intents)."""
from __future__ import annotations

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

    def account_trades(self, address: str, pages: int = 3, timeout: float | None = None, retry: bool = True) -> list:
        items, cursor = [], None
        for _ in range(pages):
            data = self.get(f"/query/accounts/{address.lower()}/trades",
                            params={"cursor": cursor} if cursor else None, timeout=timeout, retry=retry)
            items.extend(_list_payload(data))
            cursor = data.get("nextCursor") if isinstance(data, dict) else None
            if not cursor:
                break
        return items

    def deposit_address(self, address: str) -> str | None:
        r = self._request("POST", "/funding/deposits/check", json={"user": address.lower()})
        if not r.ok:
            return None
        return (r.json() or {}).get("proxy")

    # --- writes ------------------------------------------------------------
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
