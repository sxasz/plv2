"""Book mirror: snapshots, deltas, dirty-on-anomaly, REST resync (spec §10)."""

from __future__ import annotations

from typing import Any

import orjson

from polymkt_bot.feeds.polymarket_clob_ws import ClobMarketFeed, OrderBook

from .conftest import UP


def snapshot_event(token: str = UP) -> dict[str, Any]:
    return {
        "event_type": "book",
        "asset_id": token,
        "bids": [{"price": "0.90", "size": "100"}, {"price": "0.89", "size": "50"}],
        "asks": [{"price": "0.92", "size": "80"}, {"price": "0.93", "size": "20"}],
        "hash": "h1",
    }


def make_feed() -> ClobMarketFeed:
    feed = ClobMarketFeed("wss://example.invalid/ws/market")
    feed.track([UP])
    return feed


def test_snapshot_clears_dirty_and_orders_levels() -> None:
    feed = make_feed()
    book = feed.books[UP]
    assert book.dirty  # dirty until first snapshot
    feed.on_message(orjson.dumps(snapshot_event()).decode(), 1, 1)
    assert not book.dirty
    assert book.best_bid() == (0.90, 100.0)
    assert book.best_ask() == (0.92, 80.0)
    top = book.top()
    assert [lv.price for lv in top.asks_top3] == [0.92, 0.93]


def test_price_change_deltas_and_level_removal() -> None:
    feed = make_feed()
    feed.on_message(orjson.dumps(snapshot_event()).decode(), 1, 1)
    ev = {
        "event_type": "price_change",
        "asset_id": UP,
        "changes": [
            {"price": "0.92", "side": "SELL", "size": "0"},  # remove level
            {"price": "0.91", "side": "BUY", "size": "40"},  # new bid
        ],
    }
    feed.on_message(orjson.dumps(ev).decode(), 2, 2)
    book = feed.books[UP]
    assert book.best_ask() == (0.93, 20.0)
    assert book.best_bid() == (0.91, 40.0)
    assert not book.dirty


def test_crossed_book_marks_dirty() -> None:
    feed = make_feed()
    feed.on_message(orjson.dumps(snapshot_event()).decode(), 1, 1)
    ev = {
        "event_type": "price_change",
        "asset_id": UP,
        "changes": [{"price": "0.95", "side": "BUY", "size": "10"}],  # bid > ask
    }
    feed.on_message(orjson.dumps(ev).decode(), 2, 2)
    assert feed.books[UP].dirty


def test_malformed_delta_marks_dirty_not_crash() -> None:
    feed = make_feed()
    feed.on_message(orjson.dumps(snapshot_event()).decode(), 1, 1)
    ev = {"event_type": "price_change", "asset_id": UP, "changes": [{"nope": 1}]}
    feed.on_message(orjson.dumps(ev).decode(), 2, 2)
    assert feed.books[UP].dirty


def test_unknown_token_ignored() -> None:
    feed = make_feed()
    feed.on_message(orjson.dumps(snapshot_event("999")).decode(), 1, 1)
    assert UP in feed.books and "999" not in feed.books


async def test_rest_resync_restores_dirty_book() -> None:
    class FakeRest:
        async def get_book(self, token_id: str) -> tuple[list[Any], list[Any]] | None:
            return (
                [{"price": "0.88", "size": "10"}],
                [{"price": "0.91", "size": "12"}],
            )

    feed = ClobMarketFeed("wss://example.invalid/ws/market", rest=FakeRest())
    feed.track([UP])
    assert feed.books[UP].dirty
    await feed.rest_resync()
    book = feed.books[UP]
    assert not book.dirty
    assert book.best_bid() == (0.88, 10.0)
    assert book.best_ask() == (0.91, 12.0)


def test_trade_prints_forwarded() -> None:
    feed = make_feed()
    seen = []
    feed.on_trade = seen.append
    ev = {
        "event_type": "last_trade_price",
        "asset_id": UP,
        "price": "0.92",
        "size": "15",
        "side": "BUY",
        "timestamp": "1700000000000",
    }
    feed.on_message(orjson.dumps(ev).decode(), 5, 5)
    assert len(seen) == 1
    assert seen[0].price == 0.92 and seen[0].size == 15.0


def test_book_object_never_trusts_crossed_snapshot() -> None:
    book = OrderBook(UP)
    book.apply_snapshot([{"price": "0.95", "size": "1"}], [{"price": "0.94", "size": "1"}], "h", 1)
    assert book.dirty
