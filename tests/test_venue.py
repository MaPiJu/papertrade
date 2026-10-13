"""LiveVenue intent construction, without network."""
import json
import os

from eth_account import Account

from ptbot.constants import DOMAIN_EXCHANGE, DOMAIN_STAKING, SCALE
from ptbot.signing import recover_signer
from ptbot.venue import LiveVenue, Wallet

DOMAINS = {"exchange": DOMAIN_EXCHANGE, "staking": DOMAIN_STAKING}


class FakeApi:
    def __init__(self):
        self.sent = []

    def submit(self, body):
        self.sent.append(body)
        return {"intentId": "0x" + "ab" * 32}


def make(tmp_path, use_session_key=True):
    wallets = {"A": Wallet("A", "0x" + "11" * 32), "B": Wallet("B", "0x" + "22" * 32)}
    api = FakeApi()
    venue = LiveVenue(wallets, api, evm=None, hl=None, hl_api_url="http://unused", domains=DOMAINS,
                      state_dir=tmp_path, use_session_key=use_session_key)
    return venue, api


def test_open_registers_a_session_key_then_signs_with_it(tmp_path, monkeypatch):
    monkeypatch.setattr("ptbot.venue.time.sleep", lambda s: None)
    venue, api = make(tmp_path)
    venue.open("A", "BTC", True, 100.0, 100, 120)
    reg, opened = api.sent
    assert reg["action"] == "registerSessionKey"
    assert recover_signer("RegisterSessionKey", reg, DOMAINS) == venue.addr("A")
    session_addr = Account.from_key(json.loads((tmp_path / "session_keys.json").read_text())
                                    [venue.addr("A").lower()]["key"]).address
    assert reg["signed"]["key"] == session_addr.lower()
    assert opened["signed"]["size"] == str(100 * SCALE)
    assert opened["signed"]["user"] == venue.addr("A").lower()
    assert recover_signer("OpenPosition", opened, DOMAINS) == session_addr
    assert oct(os.stat(tmp_path / "session_keys.json").st_mode)[-3:] == "600"
    venue.close("A", [7])                     # key reused, not registered again
    assert [b["action"] for b in api.sent] == ["registerSessionKey", "openPosition", "closePositions"]


def test_stake_and_withdraw_are_signed_by_the_main_wallet(tmp_path):
    venue, api = make(tmp_path, use_session_key=False)
    venue.stake("B", 1234 * SCALE)
    venue.withdraw_to_core("B", 65.43)
    stake, wd = api.sent
    assert recover_signer("Stake", stake, DOMAINS) == venue.addr("B")
    assert recover_signer("WithdrawToCore", wd, DOMAINS) == venue.addr("B")
    assert wd["signed"]["amount"] == str(6543 * SCALE // 100)
