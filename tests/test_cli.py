"""Startup safety checks of `python -m ptbot run`."""
import json
import time
import types

from ptbot.__main__ import startup_problems
from ptbot.strategy import Params


class Api:
    def __init__(self, deposit, server_offset=0.0, queue=0.0):
        self.deposit, self.offset, self.queue = deposit, server_offset, queue

    def deposit_address(self, addr):
        return self.deposit[addr]

    def queue_total(self):
        return self.queue

    def server_time(self):
        return time.time() - self.offset


class Evm:
    def __init__(self, size=32):
        self.size = size

    def balances_return_size(self, addr):
        return self.size

    def block_time(self):
        return time.time() - 1


class Venue:
    def __init__(self, api, evm, keys=True, queued_ok=True):
        self.api, self.evm, self._keys, self.queued_supported = api, evm, keys, queued_ok

    def keys_ready(self):
        return self._keys

    def queued(self, w):
        return 0.0


def settings(tmp_path, dep_a="0xda", dep_b="0xdb", one_wallet=False):
    wallets = {"A": types.SimpleNamespace(address="0xA", deposit_address=dep_a),
               "B": types.SimpleNamespace(address="0xB", deposit_address=dep_b)}
    return types.SimpleNamespace(wallets=wallets, state_dir=tmp_path, params=Params(one_wallet=one_wallet))


def passed_preflight(tmp_path):
    (tmp_path / "preflight").mkdir()
    for w in "AB":
        verdict = [["PASS", "position visible after 4s"], ["PASS", "balances() excludes locked margin"]]
        (tmp_path / "preflight" / f"tiny_trade_{w}.json").write_text(json.dumps({"verdict": verdict}))


def test_everything_ready(tmp_path):
    passed_preflight(tmp_path)
    venue = Venue(Api({"0xA": "0xda", "0xB": "0xdb"}), Evm())
    assert startup_problems(settings(tmp_path), venue) == []


def test_each_missing_check_blocks_the_start(tmp_path):
    venue = Venue(Api({"0xA": "0xda", "0xB": "0xff"}, server_offset=40, queue=12.0), Evm(size=64),
                  keys=False, queued_ok=False)
    problems = " | ".join(startup_problems(settings(tmp_path), venue))
    for expected in ("register", "mini-position de test du wallet A", "mini-position de test du wallet B",
                     "DEPOSIT_ADDRESS_B", "64 octets", "file de profits", "horloge"):
        assert expected in problems, expected


def test_a_failed_tiny_trade_blocks_the_start(tmp_path):
    passed_preflight(tmp_path)
    bad = {"verdict": [["PASS", "position visible after 4s"], ["FAIL", "balances() excludes locked margin"]]}
    (tmp_path / "preflight" / "tiny_trade_B.json").write_text(json.dumps(bad))
    venue = Venue(Api({"0xA": "0xda", "0xB": "0xdb"}), Evm())
    assert any("wallet B en échec" in p for p in startup_problems(settings(tmp_path), venue))


def test_one_wallet_mode_needs_the_straddle_test_but_not_b(tmp_path):
    (tmp_path / "preflight").mkdir()
    ok = {"verdict": [["PASS", "position visible after 4s"]]}
    (tmp_path / "preflight" / "tiny_trade_A.json").write_text(json.dumps(ok))
    venue = Venue(Api({"0xA": "0xda", "0xB": "0xdb"}), Evm())
    problems = " | ".join(startup_problems(settings(tmp_path, one_wallet=True), venue))
    assert "--straddle" in problems and "wallet B" not in problems
    straddle = {"verdict": [["PASS", "long and short coexist as two positions on one wallet"]]}
    (tmp_path / "preflight" / "straddle_A.json").write_text(json.dumps(straddle))
    assert startup_problems(settings(tmp_path, one_wallet=True), venue) == []
