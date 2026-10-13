"""EIP-712 intents for the PaperTrade relayer (POST /intents).

Wire format (from the frontend): every message field as a string (uints as
decimal strings, addresses lower-cased, bools kept), plus r, s and v ("27"/"28").
"""
from __future__ import annotations

import time
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

from .constants import INTENT_ACTION, INTENT_DOMAIN_KEY, INTENT_TYPES

EIP712_DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


class NonceSource:
    """Client-side nonces: the server only rejects reused ones. Millisecond
    timestamps keep them unique across restarts; the counter keeps them
    strictly increasing within one process."""

    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        self._last = max(int(time.time() * 1000), self._last + 1)
        return self._last


def _for_hash(soltype: str, value: Any) -> Any:
    if soltype == "bool":
        return bool(value)
    if soltype == "address" or soltype.startswith("bytes"):
        return str(value).lower()
    if soltype.endswith("[]"):
        return [int(v) for v in value]
    if soltype.startswith(("uint", "int")):
        return int(value)
    return value


def _for_wire(soltype: str, value: Any) -> Any:
    if soltype == "bool":
        return bool(value)
    if soltype == "address" or soltype.startswith("bytes"):
        return str(value).lower()
    if soltype.endswith("[]"):
        return [str(int(v)) for v in value]
    if soltype.startswith(("uint", "int")):
        return str(int(value))
    return value


def build_intent(signer_key: str, intent_type: str, fields: dict, domains: dict) -> dict:
    """Signs an intent and returns the POST /intents body: {"action", "signed"}."""
    types = INTENT_TYPES[intent_type]
    missing = [f["name"] for f in types if f["name"] not in fields]
    if missing:
        raise ValueError(f"{intent_type}: missing fields {missing}")
    domain = domains[INTENT_DOMAIN_KEY[intent_type]]
    typed = {
        "types": {"EIP712Domain": EIP712_DOMAIN_TYPE, intent_type: types},
        "domain": domain,
        "primaryType": intent_type,
        "message": {f["name"]: _for_hash(f["type"], fields[f["name"]]) for f in types},
    }
    sig = Account.sign_message(encode_typed_data(full_message=typed), private_key=signer_key)
    v = sig.v if sig.v in (27, 28) else 27 + (sig.v % 2)
    signed = {f["name"]: _for_wire(f["type"], fields[f["name"]]) for f in types}
    signed.update({"r": "0x" + format(sig.r, "064x"), "s": "0x" + format(sig.s, "064x"), "v": str(v)})
    return {"action": INTENT_ACTION[intent_type], "signed": signed}


def recover_signer(intent_type: str, body: dict, domains: dict) -> str:
    """Recovers the address that signed a wire intent (used by tests and checks)."""
    types = INTENT_TYPES[intent_type]
    s = body["signed"]
    typed = {
        "types": {"EIP712Domain": EIP712_DOMAIN_TYPE, intent_type: types},
        "domain": domains[INTENT_DOMAIN_KEY[intent_type]],
        "primaryType": intent_type,
        "message": {f["name"]: _for_hash(f["type"], s[f["name"]]) for f in types},
    }
    vrs = (int(s["v"]), int(s["r"], 16), int(s["s"], 16))
    return Account.recover_message(encode_typed_data(full_message=typed), vrs=vrs)
