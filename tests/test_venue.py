"""LiveVenue intent construction, without network."""
import json
import os

from eth_account import Account

from ptbot.constants import DOMAIN_CANCEL, DOMAIN_EXCHANGE, DOMAIN_STAKING, SCALE
from ptbot.papertrade_api import IntentRejected
from ptbot.signing import recover_signer
from ptbot.venue import LiveVenue, Wallet

DOMAINS = {"exchange": DOMAIN_EXCHANGE, "staking": DOMAIN_STAKING, "cancel": DOMAIN_CANCEL}


class FakeApi:
    def __init__(self):
        self.sent = []
        self.refuse = set()     # session-key addresses the relayer refuses
        self.cancels = []
        self.register_error = None

    def submit(self, body):
        if body["action"] == "registerSessionKey" and self.register_error:
            raise self.register_error
        if body["action"] in ("openPosition", "closePositions"):
            signer = recover_signer("OpenPosition" if body["action"] == "openPosition" else "Close", body, DOMAINS)
            if signer in self.refuse:
                raise IntentRejected("closePositions refusé (HTTP 403): unauthorized session key")
        self.sent.append(body)
        return {"intentId": "0x" + "ab" * 32}

    def cancel(self, intent_id, signed):
        self.cancels.append((intent_id, signed))
        return {"ok": True}


def make(tmp_path, use_session_key=True):
    wallets = {"A": Wallet("A", "0x" + "11" * 32), "B": Wallet("B", "0x" + "22" * 32)}
    api = FakeApi()
    venue = LiveVenue(wallets, api, evm=None, hl=None, hl_api_url="http://unused", domains=DOMAINS,
                      state_dir=tmp_path, use_session_key=use_session_key)
    return venue, api


def session_address(tmp_path, venue, w, which="current"):
    entry = json.loads((tmp_path / "session_keys.json").read_text())[venue.addr(w).lower()]
    return Account.from_key((entry if which == "current" else entry["prev"][0])["key"]).address


def test_register_then_trade_with_the_session_key(tmp_path):
    import pytest

    venue, api = make(tmp_path)
    with pytest.raises(IntentRejected):           # no key yet: never trades with an unregistered key
        venue.open("A", "BTC", True, 100.0, 100, 120)
    assert [b["action"] for b in api.sent] == ["registerSessionKey"]
    assert venue.register_keys() and venue.keys_ready()
    api.sent.clear()
    venue.open("A", "BTC", True, 100.0, 100, 120)
    opened = api.sent[0]
    assert opened["signed"]["size"] == str(100 * SCALE)
    assert opened["signed"]["user"] == venue.addr("A").lower()
    assert recover_signer("OpenPosition", opened, DOMAINS) == session_address(tmp_path, venue, "A")
    assert oct(os.stat(tmp_path / "session_keys.json").st_mode)[-3:] == "600"
    venue.close("A", [7])                         # key reused, not registered again
    assert [b["action"] for b in api.sent] == ["openPosition", "closePositions"]


def test_refused_key_falls_back_to_the_previous_one_then_re_registers(tmp_path):
    import pytest

    venue, api = make(tmp_path)
    venue.register_keys()
    first = session_address(tmp_path, venue, "A")
    venue._last_register.clear()
    venue.register_keys()                         # renewal: the new key may not be active yet
    new = session_address(tmp_path, venue, "A")
    assert session_address(tmp_path, venue, "A", "prev") == first
    api.refuse = {new}
    api.sent.clear()
    venue.close("A", [7])                         # refused with the new key, accepted with the previous one
    assert recover_signer("Close", api.sent[-1], DOMAINS) == first
    api.refuse = {new, first}                     # e.g. the website registered its own key
    api.sent.clear()
    venue._last_register.clear()                  # (re-registration is limited to one a minute)
    with pytest.raises(IntentRejected):
        venue.close("A", [7])
    assert [b["action"] for b in api.sent] == ["registerSessionKey"]   # a fresh key for the next attempt


def test_older_keys_stay_usable_when_the_relayer_lags(tmp_path):
    import time

    import pytest

    venue, api = make(tmp_path)
    addresses = []
    for _ in range(7):                            # a lagging relayer: several registrations in flight
        venue._last_register.clear()
        assert venue._register("A")
        addresses.append(session_address(tmp_path, venue, "A"))
    path = tmp_path / "session_keys.json"
    entry = json.loads(path.read_text())[venue.addr("A").lower()]
    assert len(entry["prev"]) == 4                # bounded history, newest first
    api.refuse = set(addresses) - {addresses[3]}  # the relayer activated an older registration
    api.sent.clear()
    venue.close("A", [7])
    assert recover_signer("Close", api.sent[-1], DOMAINS) == addresses[3]

    keys = json.loads(path.read_text())          # expired keys are dropped at the next registration
    keys[venue.addr("A").lower()]["prev"][0]["expiry"] = time.time() - 1
    path.write_text(json.dumps(keys))
    venue._last_register.clear()
    venue._register("A")
    prev = json.loads(path.read_text())[venue.addr("A").lower()]["prev"]
    assert addresses[5] not in [p["address"] for p in prev] and len(prev) == 4

    before = path.read_text()                     # a definitive refusal leaves the file as it was
    api.register_error = IntentRejected("registerSessionKey refusé (HTTP 400): bad signature")
    venue._last_register.clear()
    assert not venue._register("A")
    assert path.read_text() == before
    api.register_error = RuntimeError("HTTP 502")  # no clear answer: the new key may be active, keep it
    venue._last_register.clear()
    assert not venue._register("A")
    entry = json.loads(path.read_text())[venue.addr("A").lower()]
    assert entry["address"] not in before and len(entry["prev"]) == 4

    keys = {venue.addr("B").lower(): {"key": "0x" + "33" * 32, "address": "x", "expiry": time.time() + 9e5,
                                      "prev": {"key": "0x" + "44" * 32, "address": "y", "expiry": time.time() + 9e5}}}
    path.write_text(json.dumps(keys))             # a file written by the previous version (one dict)
    assert venue._usable_keys("B") == ["0x" + "33" * 32, "0x" + "44" * 32]
    api.refuse |= {Account.from_key("0x" + "33" * 32).address, Account.from_key("0x" + "44" * 32).address}
    api.register_error = None
    api.sent.clear()
    with pytest.raises(IntentRejected):
        venue.open("B", "BTC", False, 10.0, 100, 120)
    assert [b["action"] for b in api.sent] == ["registerSessionKey"]
    assert len(json.loads(path.read_text())[venue.addr("B").lower()]["prev"]) == 2


def test_cancel_is_signed_by_the_main_wallet_with_the_cancel_domain(tmp_path):
    venue, api = make(tmp_path)
    intent = "0x" + "cd" * 32
    venue.cancel("B", intent)
    (iid, signed), = api.cancels
    assert iid == intent and signed["intentId"] == intent
    assert recover_signer("CancelIntent", {"signed": signed}, DOMAINS) == venue.addr("B")


def test_stake_and_withdraw_are_signed_by_the_main_wallet(tmp_path):
    venue, api = make(tmp_path, use_session_key=False)
    venue.stake("B", 1234 * SCALE)
    venue.withdraw_to_core("B", 65.43)
    stake, wd = api.sent
    assert recover_signer("Stake", stake, DOMAINS) == venue.addr("B")
    assert recover_signer("WithdrawToCore", wd, DOMAINS) == venue.addr("B")
    assert wd["signed"]["amount"] == "65430000"                 # USDC units of 10^-6, as on the website
    assert wd["withdrawal"] == {"route": "hypercore"} and "withdrawal" not in stake


def test_queued_raises_on_network_errors_and_reads_zero_only_when_unsupported(tmp_path):
    import pytest
    import requests

    from ptbot.chain import ContractCallError

    class Evm:
        def __init__(self, exc):
            self.exc = exc

        def queued_usdc(self, user):
            raise self.exc

    venue, _ = make(tmp_path)
    venue.evm = Evm(requests.ReadTimeout("timed out"))
    with pytest.raises(requests.ReadTimeout):         # transient: the bot retries instead of booking 0
        venue.queued("A")
    venue.evm = Evm(ContractCallError("eth_call userQueuedTotal(address): {'message': 'rate limited'}"))
    with pytest.raises(ContractCallError):
        venue.queued("A")
    venue.evm = Evm(ContractCallError("eth_call userQueuedTotal(address): execution reverted"))
    assert venue.queued("A") == 0.0                   # the getter does not exist on this contract
    assert venue.queued("B") == 0.0


def test_eip712_domain_absent_vs_unreachable(monkeypatch):
    import pytest
    import requests

    from ptbot.chain import ContractCallError, HyperEvm

    monkeypatch.setattr("ptbot.chain.time.sleep", lambda s: None)
    evm = HyperEvm("http://unused")

    def reverts(*a, **k):
        raise ContractCallError("eth_call eip712Domain(): execution reverted")

    def empty(*a, **k):
        return b""

    def down(*a, **k):
        raise requests.ConnectionError("429")

    evm.call_raw = reverts
    assert evm.eip712_domain("0x" + "11" * 20) is None
    evm.call_raw = empty
    assert evm.eip712_domain("0x" + "11" * 20) is None
    evm.call_raw = down
    with pytest.raises(RuntimeError):            # never fall back to a guessed domain because the RPC was down
        evm.eip712_domain("0x" + "11" * 20)


def test_hypercore_sends_use_send_asset_and_fall_back_to_spot_send():
    from ptbot.constants import HL_USDC_TOKEN
    from ptbot.hyperliquid import HyperliquidTransfers

    class Exchange:
        def __init__(self, send_asset_reply):
            self.calls, self.reply = [], send_asset_reply

        def send_asset(self, destination, source_dex, destination_dex, token, amount):
            self.calls.append(("sendAsset", destination, source_dex, destination_dex, token, amount))
            return self.reply

        def spot_transfer(self, amount, destination, token):
            self.calls.append(("spotSend", destination, amount))
            return {"status": "ok"}

    t = HyperliquidTransfers("0x" + "11" * 32, "https://api.hyperliquid.xyz")
    t._ex = Exchange({"status": "ok", "response": {"type": "default"}})
    t.spot_send_usdc("0xdep", 15.0)
    assert t._ex.calls == [("sendAsset", "0xdep", "spot", "spot", HL_USDC_TOKEN, 15.0)]   # unified accounts
    t._ex = Exchange({"status": "err", "response": "unknown action"})
    t.spot_send_usdc("0xdep", 15.0)
    assert [c[0] for c in t._ex.calls] == ["sendAsset", "spotSend"]
