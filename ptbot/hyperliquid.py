"""Hyperliquid (HyperCore): BBO mid prices, USDC balances and transfers.

PaperTrade opens and closes at the Hyperliquid BBO mid, so the bot watches the
same price. Transfers (spotSend, usdClassTransfer) go through the official
hyperliquid-python-sdk, which handles the signing.
"""
from __future__ import annotations

import logging

import requests

from .constants import HL_USDC_TOKEN

log = logging.getLogger(__name__)


class HyperliquidInfo:
    def __init__(self, api_url: str, timeout: float = 10.0) -> None:
        self.url = api_url.rstrip("/") + "/info"
        self.timeout = timeout
        self.http = requests.Session()

    def _post(self, body: dict):
        r = self.http.post(self.url, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def bbo_mid(self, coin: str) -> float:
        book = self._post({"type": "l2Book", "coin": coin})
        bids, asks = book["levels"]
        if not bids or not asks:
            raise RuntimeError(f"Carnet {coin} vide")
        return (float(bids[0]["px"]) + float(asks[0]["px"])) / 2

    def spot_usdc(self, user: str) -> float:
        st = self._post({"type": "spotClearinghouseState", "user": user})
        for b in st.get("balances", []):
            if b.get("coin") == "USDC":
                return float(b.get("total", 0)) - float(b.get("hold", 0))
        return 0.0

    def perp_withdrawable(self, user: str) -> float:
        st = self._post({"type": "clearinghouseState", "user": user})
        return float(st.get("withdrawable", 0) or 0)


class HyperliquidTransfers:
    """Signed HyperCore actions for one wallet (lazy: the SDK fetches metadata on init)."""

    def __init__(self, private_key: str, api_url: str) -> None:
        self._key = private_key
        self._url = api_url
        self._ex = None

    def _exchange(self):
        if self._ex is None:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            self._ex = Exchange(Account.from_key(self._key), self._url)
        return self._ex

    @staticmethod
    def _check(resp) -> None:
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(f"Hyperliquid a refusé l'action: {resp}")

    def spot_send_usdc(self, destination: str, amount: float) -> None:
        self._check(self._exchange().spot_transfer(round(amount, 2), destination, HL_USDC_TOKEN))

    def perp_to_spot(self, amount: float) -> None:
        self._check(self._exchange().usd_class_transfer(round(amount, 2), to_perp=False))
