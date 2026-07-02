"""Strategy logic: sniper trigger conditions, scalper impulse detection."""

from __future__ import annotations

from dataclasses import replace

from polymkt_bot.config import ModeAConfig, ModeBConfig, SizingConfig
from polymkt_bot.exec.fees import FeeModel
from polymkt_bot.markets.session import MarketSession, Phase
from polymkt_bot.strategy.base import DecisionContext
from polymkt_bot.strategy.scalper import ScalperStrategy
from polymkt_bot.strategy.sniper import SniperStrategy
from polymkt_bot.types import MarketMeta

from .conftest import WS, make_fair, make_top


def make_ctx(
    session: MarketSession,
    fee_model: FeeModel,
    now_s: float = WS + 290.0,
    p_up: float = 0.97,
    up_ask: float = 0.94,
    up_ask_size: float = 200.0,
    in_tie_band: bool = False,
    binance_mid: float = 100_200.0,
) -> DecisionContext:
    session.set_k(100_000.0, WS * 1000)
    return DecisionContext(
        session=session,
        now_s=now_s,
        tau_s=session.tau(now_s),
        phase=session.phase(now_s),
        k=session.k,
        s_cl=100_190.0,
        binance_mid=binance_mid,
        fair=make_fair(p_up=p_up, in_tie_band=in_tie_band),
        up_top=make_top(ask=up_ask, ask_size=up_ask_size),
        down_top=make_top(token=session.meta.down_token, bid=0.05, ask=0.08),
        fee_model=fee_model,
        chainlink_age_ms=100.0,
        binance_age_ms=50.0,
        book_dirty=False,
    )


def sniper() -> SniperStrategy:
    return SniperStrategy(ModeAConfig(), SizingConfig())


class TestSniper:
    def test_fires_on_confident_cheap_ask(
        self, session: MarketSession, fee_model: FeeModel
    ) -> None:
        d = sniper().evaluate(make_ctx(session, fee_model))
        assert len(d.intents) == 1
        it = d.intents[0]
        assert it.outcome == "UP" and it.side == "BUY" and it.tif.value == "FAK"
        assert it.price == 0.94
        # edge = p − ask − fee − buffer, all spelled out in the reason line.
        assert it.edge_net > 0.02
        assert "fee=" in it.reason

    def test_one_shot_per_window(self, session: MarketSession, fee_model: FeeModel) -> None:
        s = sniper()
        assert s.evaluate(make_ctx(session, fee_model)).intents
        assert not s.evaluate(make_ctx(session, fee_model)).intents

    def test_respects_phase(self, session: MarketSession, fee_model: FeeModel) -> None:
        ctx = make_ctx(session, fee_model, now_s=WS + 100.0)  # MID, not ARMED
        assert ctx.phase is Phase.MID
        assert not sniper().evaluate(ctx).intents

    def test_needs_confidence(self, session: MarketSession, fee_model: FeeModel) -> None:
        assert not sniper().evaluate(make_ctx(session, fee_model, p_up=0.90)).intents

    def test_fires_down_side(self, session: MarketSession, fee_model: FeeModel) -> None:
        ctx = make_ctx(session, fee_model, p_up=0.03)
        ctx = replace(ctx, down_top=make_top(token=session.meta.down_token, bid=0.90, ask=0.92))
        d = sniper().evaluate(ctx)
        assert d.intents and d.intents[0].outcome == "DOWN"

    def test_price_cap(self, session: MarketSession, fee_model: FeeModel) -> None:
        assert (
            not sniper()
            .evaluate(
                make_ctx(session, fee_model, up_ask=0.96)  # > max_snipe_price 0.95
            )
            .intents
        )

    def test_edge_must_clear_fee(self, session: MarketSession, fee_model: FeeModel) -> None:
        # p=0.945 at ask 0.94: raw edge 0.005 < fee(0.94)+buffer → no trade.
        assert not sniper().evaluate(make_ctx(session, fee_model, p_up=0.945)).intents

    def test_size_capped_by_depth_participation(
        self, session: MarketSession, fee_model: FeeModel
    ) -> None:
        d = sniper().evaluate(make_ctx(session, fee_model, up_ask_size=10.0))
        assert d.intents[0].size <= 5.0  # 10 × depth_participation 0.5


class TestScalper:
    def make(self) -> ScalperStrategy:
        return ScalperStrategy(ModeBConfig(enabled=True), SizingConfig())

    def test_impulse_with_stale_book_triggers(
        self, session: MarketSession, fee_model: FeeModel, meta: MarketMeta
    ) -> None:
        s = self.make()
        t0 = WS + 100.0
        # Build history: flat mid, ask stable at 0.60.
        for i in range(5):
            ctx = make_ctx(
                session,
                fee_model,
                now_s=t0 + i * 0.5,
                p_up=0.75,
                up_ask=0.60,
                binance_mid=100_000.0,
            )
            assert not s.evaluate(ctx).intents
        # Impulse: +0.1% in <1.5s while the ask hasn't repriced.
        ctx = make_ctx(
            session, fee_model, now_s=t0 + 3.0, p_up=0.75, up_ask=0.60, binance_mid=100_100.0
        )
        d = s.evaluate(ctx)
        assert d.intents and d.intents[0].outcome == "UP"
        assert d.intents[0].tif.value == "FAK"

    def test_repriced_book_means_no_trade(
        self, session: MarketSession, fee_model: FeeModel
    ) -> None:
        s = self.make()
        t0 = WS + 100.0
        for i in range(5):
            s.evaluate(
                make_ctx(
                    session,
                    fee_model,
                    now_s=t0 + i * 0.5,
                    p_up=0.75,
                    up_ask=0.60,
                    binance_mid=100_000.0,
                )
            )
        # Same impulse, but the ask already moved 0.60 → 0.70: lag is gone.
        d = s.evaluate(
            make_ctx(
                session, fee_model, now_s=t0 + 3.0, p_up=0.75, up_ask=0.70, binance_mid=100_100.0
            )
        )
        assert not d.intents

    def test_disabled_by_default(self, session: MarketSession, fee_model: FeeModel) -> None:
        s = ScalperStrategy(ModeBConfig(), SizingConfig())  # enabled=False
        assert s.evaluate(make_ctx(session, fee_model)).empty
