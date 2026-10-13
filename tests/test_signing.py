from eth_account import Account

from ptbot.constants import DOMAIN_EXCHANGE, DOMAIN_STAKING, SCALE
from ptbot.signing import NonceSource, build_intent, recover_signer

DOMAINS = {"exchange": DOMAIN_EXCHANGE, "staking": DOMAIN_STAKING}
KEY = "0x" + "11" * 32


def test_open_position_wire_format_and_signer():
    user = Account.from_key(KEY).address
    body = build_intent(KEY, "OpenPosition", {
        "user": user, "instrumentId": 0, "isLong": True, "size": 100 * SCALE, "leverage": 100,
        "useDebt": False, "nonce": 1_700_000_000_000, "deadline": 1_700_000_120}, DOMAINS)
    s = body["signed"]
    assert body["action"] == "openPosition"
    assert s["user"] == user.lower()
    assert s["size"] == str(100 * SCALE) and s["leverage"] == "100" and s["isLong"] is True
    assert s["v"] in ("27", "28") and len(s["r"]) == 66 and len(s["s"]) == 66
    assert recover_signer("OpenPosition", body, DOMAINS) == user


def test_close_ids_are_strings():
    body = build_intent(KEY, "Close", {"user": Account.from_key(KEY).address, "positionIds": [42, 7],
                                       "nonce": 1, "deadline": 2}, DOMAINS)
    assert body["action"] == "closePositions"
    assert body["signed"]["positionIds"] == ["42", "7"]


def test_stake_uses_staking_domain():
    user = Account.from_key(KEY).address
    body = build_intent(KEY, "Stake", {"user": user, "amount": 1000 * SCALE, "nonce": 1, "deadline": 2}, DOMAINS)
    assert body["action"] == "paperStake"
    assert recover_signer("Stake", body, DOMAINS) == user
    wrong = {"exchange": DOMAIN_EXCHANGE, "staking": DOMAIN_EXCHANGE}
    assert recover_signer("Stake", body, wrong) != user


def test_missing_field_is_rejected():
    try:
        build_intent(KEY, "Claim", {"user": "0x0", "nonce": 1}, DOMAINS)
    except ValueError as e:
        assert "deadline" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_nonces_strictly_increase():
    n = NonceSource()
    values = [n.next() for _ in range(50)]
    assert values == sorted(set(values))
