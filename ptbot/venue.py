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
from .constants import (FEE_ADDRESS, INSTRUMENTS, REGISTER_DEADLINE_S, SCALE, SESSION_KEY_LIFETIME_S,
                        SESSION_KEY_RENEW_S, STAKE_DEADLINE_S, WITHDRAW_DEADLINE_S)
from .hyperliquid import HyperliquidInfo, HyperliquidTransfers
from .papertrade_api import IntentRejected, PaperTradeApi
from .positions import Position, open_positions
from .signing import NonceSource, build_intent

log = logging.getLogger(__name__)

AUTH_WORDS = ("401", "403", "unauthor", "forbidden", "session", "signer", "signature", "not registered")


def _auth_refusal(e: Exception) -> bool:
    """A refusal that a fresh session key might fix (expired, revoked or not yet active)."""
    return any(w in str(e).lower() for w in AUTH_WORDS)


MAX_OLD_KEYS = 4   # older session keys kept as fallbacks while still valid


def _older_keys(entry: dict) -> list[dict]:
    prev = entry.get("prev") or []
    return [prev] if isinstance(prev, dict) else list(prev)


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
        self._last_register: dict[str, float] = {}

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

    @property
    def queued_supported(self) -> bool:
        return not self._queued_unsupported

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

    def _submit_trading(self, w: str, intent_type: str, fields: dict) -> str:
        """Opens and closes are signed by the session key. If the relayer refuses it (expired,
        revoked by the website's own key, renewal not active yet), the previous key is tried,
        then a new key is registered for the bot's next attempt (it re-sends within seconds)."""
        if not self.use_session_key:
            return self._submit(self.wallets[w].private_key, intent_type, dict(fields, nonce=self.nonces.next()))
        keys = self._usable_keys(w)
        if not keys:
            self._register(w, min_interval_s=60)
            raise IntentRejected(f"wallet {w}: aucune session key valide, une nouvelle vient d'être demandée")
        error: Exception | None = None
        for key in keys:
            try:
                return self._submit(key, intent_type, dict(fields, nonce=self.nonces.next()))
            except IntentRejected as e:
                if not _auth_refusal(e):
                    raise
                error = e
        log.error("Wallet %s: session key refusée (%s), j'en enregistre une nouvelle", w, error)
        self._register(w, min_interval_s=60)
        raise error

    def open(self, w: str, coin: str, is_long: bool, margin: float, leverage: int, deadline_s: int) -> str:
        return self._submit_trading(w, "OpenPosition", {
            "user": self.addr(w), "instrumentId": INSTRUMENTS[coin], "isLong": is_long,
            "size": int(round(margin * 100)) * SCALE // 100, "leverage": leverage, "useDebt": False,
            "deadline": self._deadline(deadline_s)})

    def close(self, w: str, position_ids: list[int]) -> str:
        return self._submit_trading(w, "Close", {
            "user": self.addr(w), "positionIds": position_ids, "deadline": self._deadline(300)})

    def cancel(self, w: str, intent_id: str) -> None:
        """Kills an order still waiting in the relayer queue (signed by the main wallet)."""
        if not (isinstance(intent_id, str) and intent_id.startswith("0x") and len(intent_id) == 66):
            raise ValueError(f"identifiant d'ordre inattendu: {intent_id!r}")
        body = build_intent(self.wallets[w].private_key, "CancelIntent",
                            {"user": self.addr(w), "intentId": intent_id, "deadline": self._deadline(300)},
                            self.domains)
        self.api.cancel(intent_id, body["signed"])

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

    def _usable_keys(self, w: str) -> list[str]:
        """The newest key, then the older ones still valid: a renewal may not be active yet, and
        when the relayer lags, the key it ends up activating may not be the newest."""
        entry = self._load_keys().get(self.addr(w).lower()) or {}
        now = time.time()
        return [k["key"] for k in [entry, *_older_keys(entry)] if k.get("key") and k.get("expiry", 0) - now > 60]

    def key_expiry(self, w: str) -> float:
        return (self._load_keys().get(self.addr(w).lower()) or {}).get("expiry", 0)

    def _register(self, w: str, min_interval_s: float = 0) -> bool:
        """Same flow as the website: a local key, registered by the main wallet, that can
        only open and close positions. Older keys are kept as fallbacks until they expire."""
        if time.time() - self._last_register.get(w, 0) < min_interval_s:
            return False
        self._last_register[w] = time.time()
        acct = Account.create()
        expiry = int(time.time()) + SESSION_KEY_LIFETIME_S
        key_hex = acct.key.hex()
        key_hex = key_hex if key_hex.startswith("0x") else "0x" + key_hex
        user = self.addr(w).lower()
        keys = self._load_keys()
        old = keys.get(user)
        valid = [k for k in [old or {}, *_older_keys(old or {})] if k.get("key") and k.get("expiry", 0) > time.time()]
        keys[user] = {"key": key_hex, "address": acct.address, "expiry": expiry,
                      "prev": [{f: k[f] for f in ("key", "address", "expiry") if f in k} for k in valid[:MAX_OLD_KEYS]]}
        self._save_keys(keys)   # before sending: a registration whose answer is lost may still go through
        try:
            self._submit(self.wallets[w].private_key, "RegisterSessionKey", {
                "user": self.addr(w), "key": acct.address, "expiry": expiry, "feeAddress": FEE_ADDRESS,
                "nonce": self.nonces.next(), "deadline": self._deadline(REGISTER_DEADLINE_S)})
        except IntentRejected as e:
            keys = self._load_keys()
            if old:
                keys[user] = old
            else:
                keys.pop(user, None)
            self._save_keys(keys)
            log.error("Wallet %s: enregistrement de session key refusé: %s", w, e)
            return False
        except Exception as e:
            log.error("Wallet %s: enregistrement de session key sans réponse claire (%s): clé gardée au cas où", w, e)
            return False
        log.info("Wallet %s: session key %s enregistrée (valide %d jours)", w, acct.address,
                 SESSION_KEY_LIFETIME_S // 86400)
        return True

    def register_keys(self) -> bool:
        return all([self._register(w) for w in self.wallets])

    def keys_ready(self, margin_s: float = 3600) -> bool:
        return not self.use_session_key or all(self.key_expiry(w) - time.time() > margin_s for w in self.wallets)

    def maintain_keys(self) -> None:
        """Renews keys close to expiry. The bot calls it only between cycles, never with a pair open."""
        if not self.use_session_key:
            return
        for w in self.wallets:
            if self.key_expiry(w) - time.time() < SESSION_KEY_RENEW_S:
                self._register(w, min_interval_s=600)
