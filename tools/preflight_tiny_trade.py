"""Supervised tiny live trade through the BOT'S OWN code (LiveVenue): 10$ margin x1000 = the
10,000$ minimum notional; worst case = the 10$ margin (liquidation sits ~0.05% away, which also
lets you capture a real liquidation event).

Validates, with real money but bounded: session-key registration and signing, relayer
acceptance, intentId format (== EIP-712 digest?), submit->visible and close->gone latencies,
the positions parser on YOUR trade (open + close/liquidation events), entry-price scale, and
whether balances() drops while the position is open (free collateral) or not (total).

Run from the bot directory, bot STOPPED, after `python -m ptbot register` (+5 minutes):
    python tools/preflight_tiny_trade.py --wallet A            # dry run: checks only
    python tools/preflight_tiny_trade.py --wallet A --i-understand-this-trades
The verdict is saved in data/preflight/tiny_trade_<wallet>.json; `python -m ptbot run` refuses
to start until both wallets have a passing one.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

MARGIN, LEVERAGE = 10.0, 1000


def read_positions(venue, w, log):
    """A failed read must not abort the script while a position is open: retry next second."""
    try:
        return venue.positions(w)
    except Exception as e:
        log(f"read failed ({e}), retrying")
        return None


def digest(intent_type, signed, domains):
    from eth_account.messages import _hash_eip191_message, encode_typed_data
    from ptbot.constants import INTENT_DOMAIN_KEY, INTENT_TYPES
    from ptbot.signing import EIP712_DOMAIN_TYPE, _for_hash
    typed = {"types": {"EIP712Domain": EIP712_DOMAIN_TYPE, intent_type: INTENT_TYPES[intent_type]},
             "domain": domains[INTENT_DOMAIN_KEY[intent_type]], "primaryType": intent_type,
             "message": {f["name"]: _for_hash(f["type"], signed[f["name"]]) for f in INTENT_TYPES[intent_type]}}
    return "0x" + _hash_eip191_message(encode_typed_data(full_message=typed)).hex()


def run(venue, w, really, log, state_file=None):
    from ptbot.positions import plausible_price
    out = {"wallet": w, "steps": []}
    verdict = []

    def step(name, **kw):
        kw["t"] = round(time.time(), 1)
        out["steps"].append({"step": name, **kw})
        log(f"{name}: {json.dumps(kw, default=str)[:300]}")

    def check(ok, what):
        verdict.append(("PASS" if ok else "FAIL", what))
        log(f"{'PASS' if ok else 'FAIL'} {what}")

    if state_file and state_file.exists():
        st = json.loads(state_file.read_text())
        if st.get("phase", "idle") != "idle" or st.get("cycle") or st.get("rebalance"):
            raise SystemExit("data/state.json shows a cycle/rebalance in progress: finish it, stop the bot first")
        if time.time() - state_file.stat().st_mtime < 30:
            raise SystemExit("data/state.json was written <30 s ago: the bot seems to be running. Create STOP first")
    sent = []
    real_submit = venue.api.submit

    def capture(body):
        resp = real_submit(body)
        sent.append((body, resp))
        return resp
    venue.api.submit = capture

    addr = venue.addr(w)
    is_long = w == "A"
    raw0 = venue.api.account_trades(addr, pages=1)
    before = venue.positions(w)
    free0 = venue.usdc(w)
    paper0 = venue.paper_raw(w)
    step("initial", address=addr, free=free0, paper=paper0 / 1e18, listed=[p.id for p in before], raw_first=raw0[:3])
    if before:
        raise SystemExit(f"positions already listed on {w}: {[p.id for p in before]} - close them on the site first")
    if not venue.keys_ready():
        raise SystemExit("no valid session key: run `python -m ptbot register` and wait ~5 minutes first")
    if free0 < MARGIN + 0.5:
        raise SystemExit(f"free balance {free0:.2f}$ < {MARGIN + 0.5}$")
    if not really:
        log("dry run OK (domains resolved, reads work). Re-run with --i-understand-this-trades to trade.")
        return out, verdict
    mid0 = venue.mid("BTC")
    t_submit = time.time()
    intent = venue.open(w, "BTC", is_long, MARGIN, LEVERAGE, 120)
    body = next(b for b, _ in sent if b["action"] == "openPosition")
    d = digest("OpenPosition", body["signed"], venue.domains)
    step("open sent", intentId=intent, local_digest=d, mid=mid0, registered_now=any(b["action"] == "registerSessionKey" for b, _ in sent))
    check(True, "relayer accepted the open (session key and signature not refused at intake)")
    verdict.append(("INFO", f"intentId == EIP-712 digest: {intent.lower() == d.lower()} (if True, a lost response can still be cancelled)"))

    pos, t_vis, free_open = None, None, None
    while time.time() - t_submit < 150:
        time.sleep(1)
        listed = read_positions(venue, w, log)
        if listed:
            pos, t_vis = listed[0], time.time()
            free_open = venue.usdc(w)
            break
    raw1 = venue.api.account_trades(addr, pages=1)
    step("after open", visible=bool(pos), latency_s=round(t_vis - t_submit, 1) if t_vis else None,
         parsed=pos.__dict__ if pos else None, free_while_open=free_open, raw_first=raw1[:4])
    if not pos:
        from ptbot.positions import ID_KEYS, _kind, _records
        free_now = venue.usdc(w)
        seen = {}
        for r in _records(raw1):
            pid = next((r[k] for k in ID_KEYS if r.get(k) not in (None, "")), None)
            if pid is not None:
                seen.setdefault(str(pid), set()).add(_kind(r))
        both = [pid for pid, kinds in seen.items() if {"open", "closed"} <= kinds]
        if both and free0 - free_now > 0.5:
            check(True, f"position {both[0]} opened AND closed/liquidated before the first read: open and "
                        f"liquidation events both understood (free {free0:.2f} -> {free_now:.2f}$). Re-run once to also "
                        "test a normal close.")
            return out, verdict
        check(False, f"position visible through the parser within 150 s (free {free0:.2f} -> {free_now:.2f}$)")
        log("If the free balance dropped, the position EXISTS but the parser cannot see it: close it on the website now, "
            "and send raw_first to fix positions.py. Do NOT run the bot.")
        return out, verdict
    check(True, f"position visible after {t_vis - t_submit:.0f}s (keep CLOSE_MOVE_PCT margin for this latency)")
    check(pos.coin in (None, "BTC") and pos.is_long in (None, is_long), f"coin/side parsed ({pos.coin}, long={pos.is_long})")
    px = plausible_price(pos.entry, mid0)
    check(px is not None, f"entry price scale recognised (raw {pos.entry} -> {px})")
    check(free_open is not None and free0 - free_open > 0.9 * MARGIN,
          f"balances() excludes locked margin (free {free0:.2f} -> {free_open:.2f}$ while open)")

    t_close = time.time()
    try:
        venue.close(w, [pos.id])
        check(True, "relayer accepted the close")
    except Exception as e:
        check(False, f"close accepted ({e}) - close it on the website NOW")
    gone_reads, t_gone = 0, None
    while time.time() - t_close < 180:
        time.sleep(2)
        listed = read_positions(venue, w, log)
        if listed is None:
            continue
        ids = [p.id for p in listed]
        gone_reads = gone_reads + 1 if pos.id not in ids else 0
        if gone_reads >= 3:
            t_gone = time.time()
            break
    raw2 = venue.api.account_trades(addr, pages=1)
    free2 = venue.usdc(w)
    step("after close", gone=bool(t_gone), latency_s=round(t_gone - t_close, 1) if t_gone else None, free=free2,
         raw_first=raw2[:6])
    check(bool(t_gone), "position reported gone by the parser after close/liquidation (close or liquidation event understood)")
    minted = (venue.paper_raw(w) - paper0) / 1e18
    if free2 < free0 - 0.01:
        check(minted > 0, f"PAPER minted on the {free0 - free2:.2f}$ loss shows up in balanceOf: +{minted:,.1f} PAPER "
                          f"(~{minted / max(free0 - free2, 1e-9):.0f} PAPER/$; 0 here = every cycle would book 0 PAPER)")
    else:
        verdict.append(("INFO", f"trade closed without loss (free {free0:.2f} -> {free2:.2f}$): PAPER mint not tested"))
    events = [r for r in raw2 if any(str(r.get(k)) == str(pos.id) for k in r)]
    step("events of this position", events=events)
    return out, verdict


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", choices=("A", "B"), default="A")
    ap.add_argument("--i-understand-this-trades", action="store_true")
    ap.add_argument("--env", default=".env")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path.cwd()))
    from ptbot.__main__ import build_live
    from ptbot.config import load_settings
    settings = load_settings(a.env)
    venue = build_live(settings)
    print(f"domains used: {json.dumps(venue.domains)}")
    state_file, really = settings.state_dir / "state.json", a.i_understand_this_trades
    out, verdict = run(venue, a.wallet, really, print, state_file)
    out["verdict"] = verdict
    if not really:
        return
    path = settings.state_dir / "preflight" / f"tiny_trade_{a.wallet}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, default=str))
    print("\n".join(f"{v:4} {w}" for v, w in verdict))
    print(f"raw log: {path}")


if __name__ == "__main__":
    main()
