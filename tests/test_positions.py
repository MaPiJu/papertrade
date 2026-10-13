from ptbot.positions import open_positions, plausible_price


def test_open_minus_closed_and_liquidated():
    payload = {"items": [
        {"positionId": "12", "type": "open", "instrumentId": 0, "isLong": True, "margin": str(100 * 10**18),
         "leverage": 100, "entryPrice": "1000000"},
        {"positionId": "11", "type": "liquidation"},
        {"positionId": "11", "type": "open", "instrumentId": 0, "isLong": False},
        {"positionId": "10", "type": "close"},
        {"positionId": "10", "type": "open", "instrumentId": 1, "isLong": True},
    ], "nextCursor": None}
    pos = open_positions(payload)
    assert [p.id for p in pos] == [12]
    p = pos[0]
    assert (p.coin, p.is_long, p.margin, p.leverage) == ("BTC", True, 100.0, 100)


def test_nested_and_string_sides():
    payload = [
        {"event": "PositionOpened", "position": {"positionId": 5, "coin": "ETH", "side": "short"}},
        {"event": "PositionOpened", "position": {"positionId": 6, "coin": "BTC", "side": "long"},
         "closedAt": 1234},
    ]
    pos = open_positions(payload)
    assert [(p.id, p.coin, p.is_long) for p in pos] == [(5, "ETH", False)]


def test_items_without_position_id_are_ignored():
    assert open_positions({"items": [{"type": "deposit", "amount": "5"}]}) == []


def test_plausible_price_finds_the_scale():
    assert plausible_price(1_000_000, 100_000) == 100_000   # BTC priceScale 10
    assert plausible_price(100_050, 100_000) == 100_050
    assert plausible_price(5, 100_000) is None
    assert plausible_price(None, 100_000) is None
