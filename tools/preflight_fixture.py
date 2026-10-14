"""Canned responses for `preflight_live.py --fixture selftest` (offline). Two leaderboard accounts:
one served newest-first with the field names the bot expects (should PASS), one served
oldest-first with Hyperliquid-style names over 3+ pages (should FAIL)."""
import email.utils
import json
import time

from eth_abi import encode
from eth_utils import keccak

EX = "https://exchange.papertrade.xyz"
HL = "https://api.hyperliquid.xyz/info"
C = {"exchange": "0x6cd5661646289fb6e65ea5c032310fded797d0a2",
     "staking": "0xaad6c7b0cc3014fc80ffedae0ed7ce5967b0f016",
     "session_key_manager": "0x8326eb8896e358717f10502ec73ce8d28e37b108"}
GOOD = "0x" + "11" * 20
BAD = "0x" + "22" * 20
WA = "0x" + "aa" * 20
PROXY_A = "0x" + "da" * 20
S = 10 ** 18


def sel(sig):
    return "0x" + keccak(text=sig)[:4].hex()


def call_key(to, data):
    return "RPC eth_call " + json.dumps([{"data": data, "to": to}, "latest"], sort_keys=True)


def dom(name, version, contract):
    t = ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"]
    return "0x" + encode(t, [b"\x0f", name, version, 999, contract, b"\x00" * 32, []]).hex()


now = time.time()
FIXTURE = {
    "DATE": email.utils.formatdate(now - 2, usegmt=True),
    f"GET {EX}/relayer/health": {"service": "relayer-intake", "databaseReady": True, "pricesReady": True, "ok": True, "ready": True},
    "POST " + HL + " " + json.dumps({"type": "exchangeStatus"}, sort_keys=True): {"time": int(now * 1000)},
    "RPC eth_getBlockByNumber " + json.dumps(["latest", False]): {"timestamp": hex(int(now) - 1), "number": hex(20_000_100)},
    "RPC eth_blockNumber []": hex(20_000_100),
    call_key(C["exchange"], sel("eip712Domain()")): dom("PaperTrade", "1", C["exchange"]),
    call_key(C["staking"], sel("eip712Domain()")): dom("PaperStaking", "1", C["staking"]),
    call_key(C["session_key_manager"], sel("eip712Domain()")): {"__error__": "rpc error {'code': 3, 'message': 'execution reverted'}"},
    f"GET {EX}/state/trading": {"schemaVersion": 3, "indexedThroughBlock": 20_000_080, "tradingPaused": False,
                                "minimumTradeSizeRaw": str(10 * S), "minimumOpenNotionalRaw": str(10_000 * S),
                                "instruments": [[0, "BTC", 0, 10, True], [1, "ETH", 1, 100, True]],
                                "intake": [["openPosition", True], ["withdrawToCore", True]]},
    f"GET {EX}/query/protocol/queue-summary": {"ok": True, "total": str(5321 * S), "count": 12, "largest": str(900 * S)},
    f"GET {EX}/state/leaderboard/positions?window=24h": {"ok": True, "rows": [
        {"rank": 1, "address": GOOD, "instrument": "BTC", "side": "long", "leverage": 100, "margin": str(150 * S),
         "entryPrice": "1000000", "pnl": "0"},
        {"rank": 2, "address": BAD, "instrument": "BTC", "side": "short", "leverage": 50, "margin": str(500 * S),
         "entryPrice": "1001000", "pnl": "0"}]},
    f"GET {EX}/query/accounts/{GOOD}/trades": {"items": [
        {"positionId": "907", "type": "open", "instrumentId": 0, "isLong": True, "margin": str(150 * S), "leverage": 100,
         "entryPrice": "1000000", "timestamp": 1792000300000},
        {"positionId": "811", "type": "liquidation", "timestamp": 1792000200000},
        {"positionId": "811", "type": "open", "instrumentId": 0, "isLong": True, "timestamp": 1792000100000}],
        "nextCursor": None},
    f"GET {EX}/query/protocol/trades/recent": {"ok": True, "count": 2, "events": [
        {"positionId": "907", "type": "open", "timestamp": 1792000300000},
        {"positionId": "811", "type": "liquidation", "timestamp": 1792000200000}]},
    call_key(C["exchange"], sel("balances(address)") + WA[2:].rjust(64, "0")): "0x" + encode(["uint256", "uint256"], [148_500_000 * 10**12, 150 * S]).hex(),
    call_key(C["exchange"], sel("userQueuedTotal(address)") + WA[2:].rjust(64, "0")): {"__error__": "rpc error {'code': 3, 'message': 'execution reverted'}"},
    "POST " + HL + " " + json.dumps({"type": "spotClearinghouseState", "user": WA}, sort_keys=True): {"balances": [{"coin": "USDC", "total": "0.0", "hold": "0.0"}]},
    "POST " + HL + " " + json.dumps({"type": "clearinghouseState", "user": WA}, sort_keys=True): {"withdrawable": "0.0"},
    "POST " + f"{EX}/funding/deposits/check " + json.dumps({"user": WA}, sort_keys=True): {"proxy": PROXY_A},
    "POST " + HL + " " + json.dumps({"type": "spotClearinghouseState", "user": PROXY_A}, sort_keys=True): {"balances": []},
    f"GET {EX}/query/accounts/{WA}/funding": {"items": [], "coreAccount": True},
}
# BAD account: 45 events oldest-first, Hyperliquid-style names, 20 per page
bad = [{"id": str(100 + i // 2), "dir": ("Open Short" if i % 2 == 0 else "Close Short"), "coin": "BTC",
        "time": 1791900000000 + i * 60_000} for i in range(44)] + [
       {"id": "907000", "dir": "Open Short", "coin": "BTC", "time": 1792000000000}]
for p, start in enumerate((0, 20, 40)):
    key = f"GET {EX}/query/accounts/{BAD}/trades" + (f"?cursor={start}" if start else "")
    FIXTURE[key] = {"items": bad[start:start + 20], "nextCursor": str(start + 20) if start + 20 < len(bad) else None}
