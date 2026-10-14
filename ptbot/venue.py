"""LiveVenue: everything the bot does on PaperTrade, HyperEVM and Hyperliquid.

The bot only talks to this interface; `sim.SimVenue` implements the same methods
offline, so the strategy can be tested without risking money.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account

from .chain import ContractCallError, HyperEvm
from .constants import (FEE_ADDRESS, INSTRUMENTS, SCALE, SESSION_KEY_LIFETIME_S, STAKE_DEADLINE_S,
                        WITHDRAW_DEADLINE_S)
from .hyperliquid import HyperliquidInfo, HyperliquidTransfers
from .papertrade_api import PaperTradeApi
from .positions import Position, open_positions
from .signing import NonceSource, build_intent

log = logging.getLogger(__name__)


@dataclass
class Wallet:
    label: str
    private_key: str
    deposit_address: str | None = None

    @property
    def address(self) -> str:
        return Account.from_key(self.private_key).address


class LiveVenue:
    def __init__(self, wallets: dict[str, Wallet], api: PaperTradeApi, evm: HyperEvm,
                 hl: HyperliquidInfo, hl_api_url: str, domains: dict, state_dir: Path,
                 use_session_key: bool = True) -> None:
        self.wallets = wallets
        self.api = api
        self.evm = evm
        self.hl = hl
        self.domains = domains
        self.use_session_key = use_session_key
        self.nonces = NonceSource()
        self.transfers = {k: HyperliquidTransfers(w.private_key, hl_api_url) for k, w in wallets.items()}
        self._keys_file = state_dir / "session_keys.json"
        self._minimums: tuple[float, float] | None = None
        self._minimums_at = 0.0
        self._queued_unsupported = False

    # --- clock -----------------------------------------------------------
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    # --- reads -----------------------------------------------------------
    def addr(self, w: str) -> str:
        return self.wallets[w].address

    def usdc(self, w: str) -> float:
        return self.evm.usdc_balance(self.addr(w))

    def queued(self, w: str) -> float:
        """Profit waiting in the FIFO queue. A network error raises (the caller retries);
        only a getter that does not exist on the contract reads as 0 (its name comes from
        bytecode analysis)."""
        if self._queued_unsupported:
            return 0.0
        try:
            return self.evm.queued_usdc(self.addr(w))
        except ContractCallError as e:
            if "revert" not in str(e).lower():
                raise
            self._queued_unsupported = True
            log.warning("userQueuedTotal() indisponible on-chain (%s): profits en file d'attente non comptés", e)
            return 0.0

    def paper_raw(self, w: str) -> int:
        return self.evm.paper_balance_raw(self.addr(w))

    def positions(self, w: str) -> list[Position]:
        # Short timeout and no retry: the bot polls again in a few seconds, and a slow read
        # must not hold up the price checks that protect an open pair.
        return open_positions(self.api.account_trades(self.addr(w), timeout=5.0, retry=False))

    def mid(self, coin: str) -> float:
        return self.hl.bbo_mid(coin)

    def minimums(self) -> tuple[float, float]:
        if self._minimums is None or time.time() - self._minimums_at > 600:
            self._minimums, self._minimums_at = self.api.minimums(), time.time()
        return self._minimums

    def trading_paused(self) -> bool:
        return self.api.trading_paused()

    def core_spot_usdc(self, w: str) -> float:
        return self.hl.spot_usdc(self.addr(w))

    def core_perp_usdc(self, w: str) -> float:
        return self.hl.perp_withdrawable(self.addr(w))

    def deposit_address(self, w: str) -> str | None:
        return self.wallets[w].deposit_address

    # --- writes ----------------------------------------------------------
    def _deadline(self, seconds: int = 3600) -> int:
        return int(time.time()) + seconds

    def _submit(self, signer_key: str, intent_type: str, fields: dict) -> str:
        body = build_intent(signer_key, intent_type, fields, self.domains)
        resp = self.api.submit(body)
        return str(resp.get("intentId", ""))

    def _trading_key(self, w: str) -> str:
        if not self.use_session_key:
            return self.wallets[w].private_key
        return self.ensure_session_key(w)

    def open(self, w: str, coin: str, is_long: bool, margin: float, leverage: int, deadline_s: int) -> str:
        return self._submit(self._trading_key(w), "OpenPosition", {
            "user": self.addr(w), "instrumentId": INSTRUMENTS[coin], "isLong": is_long,
            "size": int(round(margin * 100)) * SCALE // 100, "leverage": leverage, "useDebt": False,
            "nonce": self.nonces.next(), "deadline": self._deadline(deadline_s)})

    def close(self, w: str, position_ids: list[int]) -> str:
        return self._submit(self._trading_key(w), "Close", {
            "user": self.addr(w), "positionIds": position_ids,
            "nonce": self.nonces.next(), "deadline": self._deadline(300)})

    def stake(self, w: str, amount_raw: int) -> str:
        return self._submit(self.wallets[w].private_key, "Stake", {
            "user": self.addr(w), "amount": amount_raw,
            "nonce": self.nonces.next(), "deadline": self._deadline(STAKE_DEADLINE_S)})

    def claim(self, w: str) -> str:
        return self._submit(self.wallets[w].private_key, "Claim", {
            "user": self.addr(w), "nonce": self.nonces.next(), "deadline": self._deadline()})

    def withdraw_to_core(self, w: str, amount: float) -> str:
        return self._submit(self.wallets[w].private_key, "WithdrawToCore", {
            "user": self.addr(w), "amount": int(round(amount * 100)) * SCALE // 100,
            "nonce": self.nonces.next(), "deadline": self._deadline(WITHDRAW_DEADLINE_S)})

    def core_perp_to_spot(self, w: str, amount: float) -> None:
        self.transfers[w].perp_to_spot(amount)

    def core_spot_send(self, w: str, destination: str, amount: float) -> None:
        self.transfers[w].spot_send_usdc(destination, amount)

    # --- session keys ----------------------------------------------------
    def _load_keys(self) -> dict:
        try:
            return json.loads(self._keys_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _save_keys(self, keys: dict) -> None:
        self._keys_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._keys_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(self._keys_file)

    def ensure_session_key(self, w: str, force: bool = False) -> str:
        """Same flow as the website: a local key, registered by the main wallet,
        that can only open and close positions. Renewed 2 days before expiry."""
        keys = self._load_keys()
        entry = keys.get(self.addr(w).lower())
        if entry and not force and entry["expiry"] - time.time() > 2 * 86400:
            return entry["key"]
        acct = Account.create()
        expiry = int(time.time()) + SESSION_KEY_LIFETIME_S
        self._submit(self.wallets[w].private_key, "RegisterSessionKey", {
            "user": self.addr(w), "key": acct.address, "expiry": expiry, "feeAddress": FEE_ADDRESS,
            "nonce": self.nonces.next(), "deadline": self._deadline(600)})
        key_hex = acct.key.hex()
        key_hex = key_hex if key_hex.startswith("0x") else "0x" + key_hex
        keys[self.addr(w).lower()] = {"key": key_hex, "address": acct.address, "expiry": expiry}
        self._save_keys(keys)
        log.info("Wallet %s: session key %s enregistrée (valide 30 jours)", w, acct.address)
        time.sleep(5)  # let the relayer process the registration before the first trade
        return keys[self.addr(w).lower()]["key"]
