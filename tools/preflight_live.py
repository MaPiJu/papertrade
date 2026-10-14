"""Read-only live preflight for the PaperTrade delta-neutral bot (no private key, no write).

Run from the bot directory with its venv:
    python tools/preflight_live.py --wallet-a 0x... --wallet-b 0x... \
        --deposit-a 0x... --deposit-b 0x... --out data/preflight/preflight.json
Self-test offline with canned responses:
    python tools/preflight_live.py --fixture selftest
Prints PASS / WARN / FAIL lines and writes every raw response to --out.

Checks: local clock offset; eip712Domain() of Exchange, PaperStaking, SessionKeyManager (raw
fields bitmap); /state/trading (paused, minimums, intake, indexer lag vs chain head);
/relayer/health; profit queue; the positions parser against OTHER traders' public data
(leaderboard addresses -> /trades, order, page size, open positions found); per-wallet
balances() raw return length, userQueuedTotal, PAPER, HyperCore spot/perp, deposit proxy
(API vs .env) and whether the proxy already exists on HyperCore.
"""
from __future__ import annotations

import argparse
import email.utils
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))           # the bot directory (for ptbot.positions)

EXCHANGE = "https://exchange.papertrade.xyz"
RPC = "https://rpc.hyperliquid.xyz/evm"
HL = "https://api.hyperliquid.xyz/info"
CONTRACTS = {"exchange": "0x6cd5661646289fb6e65ea5c032310fded797d0a2",
             "staking": "0xaad6c7b0cc3014fc80ffedae0ed7ce5967b0f016",
             "session_key_manager": "0x8326eb8896e358717f10502ec73ce8d28e37b108"}
DEFAULT_NAMES = {"exchange": ("String", "1"), "staking": ("PaperStaking", "1")}
SCALE = 10 ** 18
results = []
raw = {}


def report(level, check, detail):
    results.append((level, check, detail))
    print(f"{level:4} {check:34} {detail}")


class Net:
    def __init__(self, fixture=None):
        import requests
        self.fixture = fixture
        self.http = requests.Session()
        self.http.headers.update({"Accept": "application/json"})

    def _fx(self, key):
        if key not in self.fixture:
            raise RuntimeError(f"no fixture for {key}")
        v = self.fixture[key]
        if isinstance(v, dict) and "__error__" in v:
            raise RuntimeError(v["__error__"])
        return v

    def get(self, url, params=None):
        if self.fixture is not None:
            q = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
            return self._fx(f"GET {url}" + (f"?{q}" if q else "")), self._fx("DATE")
        r = self.http.get(url, params=params, timeout=20)
        date = r.headers.get("Date")
        r.raise_for_status()
        return r.json(), date

    def post(self, url, body):
        if self.fixture is not None:
            return self._fx(f"POST {url} {json.dumps(body, sort_keys=True)}"), self._fx("DATE")
        r = self.http.post(url, json=body, timeout=20)
        date = r.headers.get("Date")
        r.raise_for_status()
        return r.json(), date

    def rpc(self, method, params):
        if self.fixture is not None:
            return self._fx(f"RPC {method} {json.dumps(params, sort_keys=True)}")
        r = self.http.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"rpc error {body['error']}")
        return body["result"]


def selector(sig):
    from eth_utils import keccak
    return "0x" + keccak(text=sig)[:4].hex()


def eth_call(net, to, sig, arg_hex=""):
    return net.rpc("eth_call", [{"to": to, "data": selector(sig) + arg_hex}, "latest"])


def addr_arg(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def check_clock(net):
    offsets = []
    for name, url in (("papertrade", f"{EXCHANGE}/relayer/health"), ("hyperliquid", None)):
        try:
            if url:
                _, date = net.get(url)
            else:
                _, date = net.post(HL, {"type": "exchangeStatus"})
            if date:
                offsets.append((name, time.time() - email.utils.parsedate_to_datetime(date).timestamp()))
        except Exception as e:
            report("WARN", f"clock vs {name}", f"unreadable ({e})")
    try:
        blk = net.rpc("eth_getBlockByNumber", ["latest", False])
        offsets.append(("hyperevm block", time.time() - int(blk["timestamp"], 16)))
        raw["latest_block"] = blk.get("number")
    except Exception as e:
        report("WARN", "clock vs hyperevm", f"unreadable ({e})")
    for name, off in offsets:
        lvl = "PASS" if abs(off) < 5 else "WARN" if abs(off) < 30 else "FAIL"
        report(lvl, f"clock offset vs {name}", f"{off:+.1f} s (|x| must stay < 30 s: deadlines and late fills)")


def check_domains(net):
    from eth_abi import decode
    out_types = ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"]
    for key, addr in CONTRACTS.items():
        try:
            res = eth_call(net, addr, "eip712Domain()")
        except Exception as e:
            msg = str(e)
            # Absent on-chain is expected: the website's own code signs with the same names as the bot
            # (exchange String/1, PaperStaking/1, PaperTrade Intent Cancel/2, checked 14/10/2026).
            report("WARN", f"eip712Domain() {key}", f"unreadable: {msg[:90]} (revert = absent, the bot's "
                   "defaults are the website's; anything else = retry)")
            continue
        fields, name, version, chain, verifying, salt, ext = decode(out_types, bytes.fromhex(res[2:]))
        raw[f"eip712_{key}"] = {"fields": fields.hex(), "name": name, "version": version, "chainId": chain,
                                "verifyingContract": verifying, "salt": salt.hex(), "extensions": ext}
        lvl = "PASS" if fields == b"\x0f" and chain == 999 and verifying.lower() == addr else "FAIL"
        report(lvl, f"eip712Domain() {key}", f"name={name!r} version={version!r} fields=0x{fields.hex()} chain={chain} "
                                             f"contract={verifying}")
        if key in DEFAULT_NAMES and (name, version) != DEFAULT_NAMES[key]:
            report("WARN", f"domain {key} differs from guess", f"on-chain {name}/{version} vs guessed "
                   f"{'/'.join(DEFAULT_NAMES[key])}: the bot will sign with the on-chain one")


def check_state(net):
    try:
        st, _ = net.get(f"{EXCHANGE}/state/trading")
        raw["state_trading"] = st
        report("PASS" if not st.get("tradingPaused") else "WARN", "tradingPaused", str(st.get("tradingPaused")))
        mins = (int(st["minimumTradeSizeRaw"]) / SCALE, int(st["minimumOpenNotionalRaw"]) / SCALE)
        report("PASS" if mins == (10.0, 10000.0) else "WARN", "minimums (margin, notional)", str(mins))
        report("INFO", "intake (raw)", json.dumps(st.get("intake"))[:160])
        head = int(net.rpc("eth_blockNumber", []), 16)
        lag = head - int(st.get("indexedThroughBlock", 0))
        report("PASS" if lag < 30 else "WARN" if lag < 300 else "FAIL", "indexer lag",
               f"{lag} blocks behind chain head (the bot allows ~30 s before trusting the API)")
    except Exception as e:
        report("FAIL", "/state/trading", str(e)[:120])
    for path, name in (("/relayer/health", "relayer health"), ("/query/protocol/queue-summary", "profit queue")):
        try:
            data, _ = net.get(EXCHANGE + path)
            raw[name] = data
            if name == "profit queue":
                total = int(data.get("total") or 0) / SCALE
                report("WARN" if total > 0 else "PASS", name, f"{total:,.2f}$ queued in {data.get('count')} entries: "
                       "winners' profits are queued -> userQueuedTotal must be readable" if total > 0 else "empty")
            else:
                report("PASS" if data.get("ok") and data.get("ready") else "WARN", name, json.dumps(data)[:120])
        except Exception as e:
            report("WARN", name, str(e)[:120])


def _ts(item):
    for k in ("timestamp", "ts", "time", "createdAt", "blockTime", "timeMs", "block", "blockNumber"):
        v = item.get(k)
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            return float(v)
    return None


def fetch_trades(net, addr, pages=3):
    items, cursor, sizes = [], None, []
    for _ in range(pages):
        data, _ = net.get(f"{EXCHANGE}/query/accounts/{addr.lower()}/trades", {"cursor": cursor} if cursor else None)
        page = data.get("items", []) if isinstance(data, dict) else data
        sizes.append(len(page))
        items.extend(page)
        cursor = data.get("nextCursor") if isinstance(data, dict) else None
        if not cursor:
            break
    return items, sizes, cursor


def check_parser(net, max_accounts=5):
    from ptbot.positions import ID_KEYS, _kind, _records, open_positions
    try:
        lb, _ = net.get(f"{EXCHANGE}/state/leaderboard/positions", {"window": "24h"})
        rows = lb.get("rows", [])
        raw["leaderboard_positions_first_rows"] = rows[:5]
    except Exception as e:
        report("WARN", "leaderboard positions", f"unreadable ({e}); check the parser with your own manual trade")
        rows = []
    by_addr = {}
    for r in rows:
        if isinstance(r, dict) and r.get("address"):
            by_addr.setdefault(r["address"].lower(), []).append(r)
    checked = 0
    for addr, lrows in list(by_addr.items())[:max_accounts]:
        try:
            items, sizes, more = fetch_trades(net, addr)
        except Exception as e:
            report("WARN", f"trades {addr[:10]}", str(e)[:100])
            continue
        checked += 1
        recs = list(_records(items))
        with_id = sum(1 for r in recs if any(r.get(k) not in (None, "") for k in ID_KEYS))
        kinds = {}
        for r in recs:
            kinds[_kind(r)] = kinds.get(_kind(r), 0) + 1
        try:
            parsed = open_positions(items)
        except ValueError as e:
            report("FAIL", f"trades {addr[:10]} format", f"the bot's parser refuses these events: {e}")
            parsed = []
        ts = [t for t in (_ts(i) for i in items) if t is not None]
        order = "newest-first" if ts and ts == sorted(ts, reverse=True) else "oldest-first" if ts and ts == sorted(ts) else "unknown"
        raw.setdefault("trades_samples", {})[addr] = {"page_sizes": sizes, "has_more": bool(more), "first_items": items[:6],
                                                      "parsed_open": [p.__dict__ for p in parsed], "leaderboard_rows": lrows}
        lvl_ids = "PASS" if recs and with_id == len(recs) else "FAIL"
        report(lvl_ids, f"trades {addr[:10]} ids", f"{with_id}/{len(recs)} events carry a position id the parser reads")
        lvl_kind = "PASS" if kinds.get("unknown", 0) == 0 else "FAIL"
        report(lvl_kind, f"trades {addr[:10]} kinds", f"{kinds} (unknown = events the parser ignores)")
        report("WARN" if order == "oldest-first" else "PASS" if order == "newest-first" else "WARN",
               f"trades {addr[:10]} order", f"{order}, page sizes {sizes}, more pages: {bool(more)}"
               + (" -> the bot follows oldest-first feeds to their last page; check it sees new positions"
                  if order == "oldest-first" else ""))
        report("INFO", f"trades {addr[:10]} parsed open", f"{len(parsed)} open: {[(p.id, p.coin, p.is_long, p.entry) for p in parsed][:4]}"
               f" | leaderboard shows {len(lrows)} row(s): {[(r.get('instrument'), r.get('side'), r.get('entryPrice')) for r in lrows][:4]}")
    if not checked:
        report("WARN", "parser on public data", "no account checked: validate with your own manual trade + probe")
    try:
        rec, _ = net.get(f"{EXCHANGE}/query/protocol/trades/recent")
        events = rec.get("events", [])
        raw["protocol_trades_recent_first"] = events[:10]
        recs = list(_records(events))
        kinds = {}
        for r in recs:
            kinds[_kind(r)] = kinds.get(_kind(r), 0) + 1
        report("PASS" if recs and kinds.get("unknown", 0) == 0 else "WARN", "recent protocol events kinds",
               f"{kinds} over {len(recs)} events (look for a liquidation example in --out)")
    except Exception as e:
        report("WARN", "recent protocol events", str(e)[:100])


def check_wallet(net, label, addr, deposit_env):
    w = {}
    try:
        res = eth_call(net, CONTRACTS["exchange"], "balances(address)", addr_arg(addr))
        nbytes = (len(res) - 2) // 2
        w["balances_raw"] = res
        report("PASS" if nbytes == 32 else "FAIL", f"{label} balances() return size",
               f"{nbytes} bytes ({'single uint256' if nbytes == 32 else 'STRUCT: the bot reads only the first word'})")
        report("INFO", f"{label} balances()", f"{int(res[2:66], 16) / SCALE:,.6f} USDC (compare with the site's 'available')")
    except Exception as e:
        report("FAIL", f"{label} balances()", str(e)[:100])
    for sig, name in (("userQueuedTotal(address)", "userQueuedTotal"),):
        try:
            res = eth_call(net, CONTRACTS["exchange"], sig, addr_arg(addr))
            report("PASS", f"{label} {name}", f"{int(res[2:66], 16) / SCALE:,.6f} USDC")
        except Exception as e:
            report("WARN", f"{label} {name}", f"unreadable ({str(e)[:80]}): queued profits would be booked as losses")
    try:
        spot, _ = net.post(HL, {"type": "spotClearinghouseState", "user": addr})
        perp, _ = net.post(HL, {"type": "clearinghouseState", "user": addr})
        usdc = next((float(b["total"]) - float(b.get("hold", 0)) for b in spot.get("balances", []) if b.get("coin") == "USDC"), 0.0)
        report("INFO", f"{label} HyperCore", f"spot USDC {usdc:.2f}, perp withdrawable {float(perp.get('withdrawable', 0) or 0):.2f}")
    except Exception as e:
        report("WARN", f"{label} HyperCore", str(e)[:100])
    try:
        dep, _ = net.post(f"{EXCHANGE}/funding/deposits/check", {"user": addr.lower()})
        proxy = (dep or {}).get("proxy")
        w["deposit_api"] = proxy
        same = bool(proxy and deposit_env and proxy.lower() == deposit_env.lower())
        report("PASS" if same else "FAIL", f"{label} deposit proxy", f"API {proxy} vs .env {deposit_env}")
        if proxy:
            st, _ = net.post(HL, {"type": "spotClearinghouseState", "user": proxy})
            known = bool(st.get("balances"))
            report("INFO" if known else "WARN", f"{label} proxy on HyperCore",
                   "has a spot history" if known else "no HyperCore balance ever seen: a first send may pay the "
                   "new-account fee -> do a small manual spotSend first")
    except Exception as e:
        report("WARN", f"{label} deposit proxy", str(e)[:100])
    try:
        fund, _ = net.get(f"{EXCHANGE}/query/accounts/{addr.lower()}/funding")
        w["funding_first_items"] = (fund.get("items") or [])[:5]
        report("INFO", f"{label} funding", f"coreAccount={fund.get('coreAccount')} items={len(fund.get('items') or [])}")
    except Exception as e:
        report("WARN", f"{label} funding", str(e)[:100])
    raw[f"wallet_{label}"] = w


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet-a")
    ap.add_argument("--wallet-b")
    ap.add_argument("--deposit-a")
    ap.add_argument("--deposit-b")
    ap.add_argument("--out", default="data/preflight/preflight.json")
    ap.add_argument("--fixture", help="'selftest' or a JSON file of canned responses (offline)")
    a = ap.parse_args(argv)
    fixture = None
    if a.fixture == "selftest":
        from preflight_fixture import FIXTURE
        fixture = FIXTURE
    elif a.fixture:
        fixture = json.loads(Path(a.fixture).read_text())
    net = Net(fixture)
    check_clock(net)
    check_domains(net)
    check_state(net)
    check_parser(net)
    for label, addr, dep in (("A", a.wallet_a, a.deposit_a), ("B", a.wallet_b, a.deposit_b)):
        if addr:
            check_wallet(net, label, addr, dep)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"results": results, "raw": raw}, indent=1, default=str))
    bad = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(bad)} FAIL, {sum(1 for r in results if r[0] == 'WARN')} WARN -> raw responses in {a.out}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
