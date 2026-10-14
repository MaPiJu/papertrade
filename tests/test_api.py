"""PaperTradeApi reads that need no network."""
import json

import pytest

from ptbot.papertrade_api import PaperTradeApi


class Stream:
    def __init__(self, lines):
        self.lines, self.closed = lines, False

    def raise_for_status(self):
        pass

    def iter_lines(self, chunk_size=512):
        yield from (line.encode() for line in self.lines)

    def close(self):
        self.closed = True


def api_with(lines):
    api = PaperTradeApi("https://example.invalid")
    stream = Stream(lines)
    calls = []

    def get(url, **kw):
        calls.append((url, kw))
        return stream
    api.http.get = get
    return api, stream, calls


def test_account_live_returns_the_first_full_snapshot():
    snap = {"openPositions": [["7", 0, True, False, "817665", str(44 * 10**18), 250, "814800", 1]], "paper": {}}
    lines = [": keep-alive", "event: hello", 'data: {"x": 1}', "",
             "event: wallet", 'data: {"baseAccountRevision": 3, "openPositions": []}', "",     # a patch: skipped
             "event: wallet", "data: " + json.dumps(snap)[:20], "data: " + json.dumps(snap)[20:], "",
             "event: wallet", 'data: {"openPositions": []}', ""]
    api, stream, calls = api_with(lines)
    assert api.account_live("0xABC") == json.loads(json.dumps(snap)[:20] + "\n" + json.dumps(snap)[20:])
    assert stream.closed and calls[0][1]["params"] == {"wallet": "0xabc"} and calls[0][1]["stream"]


def test_account_live_without_a_snapshot_raises():
    api, stream, _ = api_with([": keep-alive", "event: wallet", 'data: {"baseAccountRevision": 1}', ""])
    with pytest.raises(RuntimeError):
        api.account_live("0xabc")
    assert stream.closed
