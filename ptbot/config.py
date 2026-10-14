"""Settings from .env (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .constants import EXCHANGE_BASE, HL_API, HYPEREVM_RPC, INSTRUMENTS
from .strategy import Params
from .venue import Wallet


@dataclass
class Settings:
    wallets: dict[str, Wallet]
    params: Params
    state_dir: Path
    api_url: str
    rpc_url: str
    hl_url: str
    use_session_key: bool
    open_deadline_s: int


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _pct(name: str, default: float) -> float:
    return _f(name, default) / 100


def load_settings(env_file: str = ".env", need_keys: bool = True) -> Settings:
    load_dotenv(env_file)
    wallets = {}
    for w in ("A", "B"):
        key = os.getenv(f"WALLET_{w}_PRIVATE_KEY", "").strip()
        if not key:
            if need_keys:
                raise SystemExit(f"WALLET_{w}_PRIVATE_KEY manquant dans {env_file}")
            continue
        wallets[w] = Wallet(w, key if key.startswith("0x") else "0x" + key,
                            os.getenv(f"DEPOSIT_ADDRESS_{w}", "").strip() or None)
    if need_keys and wallets["A"].address == wallets["B"].address:
        raise SystemExit("WALLET_A et WALLET_B doivent être deux adresses différentes")
    coin = os.getenv("COIN", "BTC").upper()
    if coin not in INSTRUMENTS:
        raise SystemExit(f"COIN doit être l'un de {list(INSTRUMENTS)}")
    leverage = int(_f("LEVERAGE", 100))
    params = Params(
        coin=coin,
        leverage=leverage,
        max_leverage=max(leverage, int(_f("MAX_LEVERAGE", leverage))),
        close_move=_pct("CLOSE_MOVE_PCT", 0.70),
        liq_safety=_pct("LIQ_SAFETY_PCT", 0.15),
        margin_fraction=_f("MARGIN_FRACTION", 1.0),
        max_margin_usd=_f("MAX_MARGIN_USD", 0),
        rebalance_threshold=_pct("REBALANCE_THRESHOLD_PCT", 10),
        min_rebalance_usd=_f("MIN_REBALANCE_USD", 5),
        stake_threshold=_f("STAKE_THRESHOLD_PAPER", 1000),
        max_cost_per_1000=_f("MAX_COST_PER_1000_PAPER", 7),
        cost_window=int(_f("COST_WINDOW", 1)),
    )
    return Settings(
        wallets=wallets,
        params=params,
        state_dir=Path(os.getenv("STATE_DIR", "data")),
        api_url=os.getenv("PAPERTRADE_API", EXCHANGE_BASE),
        rpc_url=os.getenv("HYPEREVM_RPC", HYPEREVM_RPC),
        hl_url=os.getenv("HYPERLIQUID_API", HL_API),
        use_session_key=os.getenv("USE_SESSION_KEY", "true").lower() in ("1", "true", "yes", "oui"),
        open_deadline_s=int(_f("OPEN_DEADLINE_S", 120)),
    )
