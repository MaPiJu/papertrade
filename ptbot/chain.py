"""Read-only HyperEVM calls (JSON-RPC eth_call, no web3 dependency)."""
from __future__ import annotations

import logging

import requests
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from .constants import EXCHANGE_CONTRACT, PAPER_STAKING, PAPER_TOKEN, PAPER_TOKENOMICS, SCALE

log = logging.getLogger(__name__)


class ContractCallError(RuntimeError):
    """The node answered the eth_call with an error (revert, missing function...)."""


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


class HyperEvm:
    def __init__(self, rpc_url: str, timeout: float = 15.0) -> None:
        self.rpc_url = rpc_url
        self.timeout = timeout
        self.http = requests.Session()

    def call(self, to: str, signature: str, arg_types: list[str], args: list, out_types: list[str]):
        data = selector(signature) + encode(arg_types, args)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": to_checksum_address(to), "data": "0x" + data.hex()}, "latest"]}
        r = self.http.post(self.rpc_url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise ContractCallError(f"eth_call {signature}: {body['error']}")
        raw = bytes.fromhex(body["result"][2:])
        return decode(out_types, raw)

    # --- PaperTrade reads ------------------------------------------------
    def usdc_balance(self, user: str) -> float:
        """Free collateral on the exchange (`balances(address)`, 18 decimals)."""
        (v,) = self.call(EXCHANGE_CONTRACT, "balances(address)", ["address"], [to_checksum_address(user)], ["uint256"])
        return v / SCALE

    def paper_balance_raw(self, user: str) -> int:
        """Unstaked PAPER held by the wallet (ERC-20 balanceOf, 18 decimals)."""
        (v,) = self.call(PAPER_TOKEN, "balanceOf(address)", ["address"], [to_checksum_address(user)], ["uint256"])
        return v

    def queued_usdc(self, user: str) -> float:
        """Profit waiting in the FIFO queue (`userQueuedTotal(address)`)."""
        (v,) = self.call(EXCHANGE_CONTRACT, "userQueuedTotal(address)", ["address"], [to_checksum_address(user)], ["uint256"])
        return v / SCALE

    def emission_per_usd(self) -> float | None:
        """PAPER minted per $1 lost: 100 while the tracked LP is under $2M, then
        100 x (120M / (120M + H))^2 with H the high-water mark of LP gains past $2M.
        Getter names come from bytecode analysis: None if they don't answer."""
        try:
            (tracked,) = self.call(PAPER_TOKENOMICS, "trackedLpUsd()", [], [], ["int256"])
            (tail,) = self.call(PAPER_TOKENOMICS, "tailProgressUsd()", [], [], ["uint256"])
        except Exception as e:
            log.debug("Émission illisible: %s", e)
            return None
        if tracked / SCALE < 2_000_000:
            return 100.0
        return 100.0 * (120e6 / (120e6 + tail / SCALE)) ** 2

    def eip712_domain(self, contract: str) -> dict | None:
        """EIP-5267 `eip712Domain()`: the domain the contract really verifies against."""
        try:
            _, name, version, chain_id, verifying, _, _ = self.call(
                contract, "eip712Domain()", [], [],
                ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"])
        except Exception as e:  # contract may not implement EIP-5267
            log.debug("eip712Domain() unavailable on %s: %s", contract, e)
            return None
        return {"name": name, "version": version, "chainId": chain_id, "verifyingContract": verifying.lower()}


def resolve_domains(evm: HyperEvm, defaults: dict) -> dict:
    """Uses the on-chain domains when readable, otherwise the reverse-engineered ones."""
    out = dict(defaults)
    for key, contract in (("exchange", EXCHANGE_CONTRACT), ("staking", PAPER_STAKING)):
        onchain = evm.eip712_domain(contract)
        if not onchain:
            log.warning("Domaine EIP-712 '%s' non lisible on-chain: valeurs par défaut %s", key, defaults[key])
            continue
        if (onchain["name"], onchain["version"]) != (defaults[key]["name"], defaults[key]["version"]):
            log.warning("Domaine EIP-712 '%s' on-chain = %s/%s (défaut %s/%s): j'utilise la version on-chain",
                        key, onchain["name"], onchain["version"], defaults[key]["name"], defaults[key]["version"])
        out[key] = onchain
    return out
