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


def test_unreadable_events_fail_closed():
    import pytest
    with pytest.raises(ValueError):            # events without any position id: format not understood
        open_positions({"items": [{"type": "deposit", "amount": "5"}]})
    with pytest.raises(ValueError):            # ids but no event kind the parser understands
        open_positions({"items": [{"positionId": 1, "foo": "bar"}]})
    assert open_positions({"items": []}) == []
    mixed = {"items": [{"type": "deposit"}, {"positionId": 3, "type": "open", "isLong": True}]}
    assert [p.id for p in open_positions(mixed)] == [3]


def test_more_event_shapes():
    hl_style = [{"positionId": "7", "dir": "Open Long", "coin": "BTC"},
                {"positionId": "8", "dir": "Open Short", "coin": "BTC"},
                {"positionId": "8", "dir": "Close Short", "coin": "BTC"}]
    assert [(p.id, p.is_long) for p in open_positions(hl_style)] == [(7, True)]
    zero_exit = [{"positionId": 9, "type": "open", "isLong": False, "exitPrice": "0.0"}]
    assert [p.id for p in open_positions(zero_exit)] == [9]       # "0.0" is not a close
    bust = [{"positionId": 4, "type": "open", "isLong": True}, {"positionId": 4, "type": "bust"}]
    assert open_positions(bust) == []
    hex_ids = [{"positionId": "0x1a", "type": "open", "side": "long"}]
    assert [p.id for p in open_positions(hex_ids)] == [26]


def test_plausible_price_finds_the_scale():
    assert plausible_price(1_000_000, 100_000) == 100_000   # BTC priceScale 10
    assert plausible_price(100_050, 100_000) == 100_050
    assert plausible_price(5, 100_000) is None
    assert plausible_price(None, 100_000) is None


def test_live_snapshot_rows():
    import pytest

    from ptbot.positions import live_positions
    snap = {"openPositions": [["673851", 0, True, False, "817665", str(44 * 10**18), 250, "814800", 1792015850000],
                              {"positionId": "0x10", "instrumentId": 1, "isLong": False, "marginRaw": str(10**19),
                               "leverage": 100, "entryPriceRaw": "241175"}],
            "paper": {"balance": "0"}, "funds": {"availableBalance": "0"}}
    a, b = live_positions(snap)
    assert (a.id, a.coin, a.is_long, a.margin, a.leverage, a.entry) == (673851, "BTC", True, 44.0, 250, 817665.0)
    assert (b.id, b.coin, b.is_long, b.margin, b.leverage) == (16, "ETH", False, 10.0, 100)
    assert live_positions({"openPositions": []}) == []
    for bad in ({"paper": {}}, {"openPositions": [["x", 0, True, False, "1", "1", 1]]}, {"openPositions": ["?"]}):
        with pytest.raises(ValueError):          # never read as "no position"
            live_positions(bad)


def test_real_trade_history_records_are_closures():
    rec = {"timestampMs": 1791981854000, "positionId": "596126", "instrumentId": 0, "symbol": "BTC",
           "priceScaleRaw": "10", "terminal": False, "retired": False, "isLong": True, "entryPriceRaw": "833785",
           "marginRaw": "40000000000000000000000", "leverage": 40, "kind": "PositionClosed",
           "openedAtMs": 1791977163000, "exitPriceRaw": "826875", "paperMintedRaw": "0"}
    assert open_positions({"items": [rec], "nextCursor": None}) == []
