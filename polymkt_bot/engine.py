"""Trading engine: event wiring, session lifecycle, decision loop (spec §3).

Hot path: feed event → evaluate() → risk gates → adapter submit. Everything
here is allocation-light and synchronous except order submission; REST never
appears in the hot path. The engine works identically in shadow-live and
replay because all time flows through the injected Clock and all fills flow
through the ExchangeAdapter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

import orjson

from .accounting.pnl import Ledger
from .clock import Clock, ntp_offset, window_start
from .config import Config
from .exec.fees import FeeModel
from .exec.order_manager import ManagedOrder, OrderManager, OrderState
from .feeds.binance_ws import BinanceFeed, BinanceState
from .feeds.polymarket_clob_ws import ClobMarketFeed, OrderBook, TradePrint
from .feeds.rtds_chainlink_ws import KCapture, OraclePrint, RtdsChainlinkFeed
from .markets.discovery import MarketDiscovery
from .markets.session import MarketSession, Phase, SessionState
from .model.fair_value import BasisTracker, FairValueModel
from .model.vol import EwmaVol
from .risk.risk_manager import GateContext, RiskManager
from .sim.shadow_exchange import ShadowExchange
from .storage.database import Database
from .storage.recorder import Recorder
from .strategy.base import DecisionContext, Strategy
from .strategy.scalper import ScalperStrategy
from .types import BookTop, Fill, OrderIntent

log = logging.getLogger(__name__)

TICK_INTERVAL_S = 0.1
RECONCILE_INTERVAL_S = 30.0
NTP_INTERVAL_S = 300.0
DECISION_LOG_IDLE_INTERVAL_NS = 1_000_000_000  # 1 s outside ARMED


class Engine:
    def __init__(
        self,
        cfg: Config,
        clock: Clock,
        binance: BinanceFeed,
        rtds: RtdsChainlinkFeed,
        clob: ClobMarketFeed,
        discovery: MarketDiscovery,
        order_manager: OrderManager,
        risk: RiskManager,
        ledger: Ledger,
        db: Database,
        decisions: Recorder,
        strategies: list[Strategy],
        shadow: ShadowExchange | None = None,
        replay_mode: bool = False,
        fee_models: dict[str, FeeModel] | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.binance = binance
        self.rtds = rtds
        self.clob = clob
        self.discovery = discovery
        self.om = order_manager
        self.risk = risk
        self.ledger = ledger
        self.db = db
        self.decisions = decisions
        self.strategies = strategies
        self.shadow = shadow
        self.replay_mode = replay_mode

        self.vol = EwmaVol(cfg.model.ewma_halflife_s, cfg.model.vol_floor_per_s)
        self.basis = BasisTracker(cfg.model.basis_window_s)
        self.fair = FairValueModel(
            self.vol,
            self.basis,
            lambda_lead=cfg.model.lambda_lead,
            p_clip=cfg.model.p_clip,
            tie_bonus=cfg.model.tie_bonus,
            k_tie=cfg.model.k_tie,
        )

        self.current: MarketSession | None = None
        self.next: MarketSession | None = None
        self.settling: dict[int, MarketSession] = {}  # window_close → session
        # token_id → FeeModel; shared with the shadow exchange when present.
        self._fee_models: dict[str, FeeModel] = fee_models if fee_models is not None else {}
        self._order_outcome: dict[str, str] = {}  # client_id → outcome
        self.ntp_offset_ms: float | None = 0.0 if replay_mode else None
        self._last_decision_log_ns = 0
        self._stopping = False
        self._bg: list[asyncio.Task[None]] = []

        # Wire feed callbacks (hot path).
        binance.on_tick = self._on_binance_tick
        rtds.on_print = self._on_oracle_print
        rtds.on_k_capture = self._on_k_capture
        clob.on_book_update = self._on_book_update
        clob.on_trade = self._on_trade
        risk.set_kill_handler(self._on_kill)
        if shadow is not None:
            shadow.on_fill = self._on_shadow_fill
            shadow.on_cancel_confirmed = self.om.confirm_canceled
            shadow.on_expired = self.om.expire_unfilled

        self.om.on_fill_applied = self._on_fill_applied

    # ------------------------------------------------------------------ feeds
    def _on_binance_tick(self, st: BinanceState, recv_mono_ns: int) -> None:
        self._pump(recv_mono_ns)

    def _on_oracle_print(self, pr: OraclePrint) -> None:
        ts_s = pr.oracle_ts_ms / 1000.0
        self.vol.update(pr.value, ts_s)
        mid = self.binance.state.mid
        self.basis.update(ts_s, pr.value, mid)
        self.fair.on_chainlink_print(pr.value, mid)
        self._pump(pr.recv_mono_ns)

    def _on_k_capture(self, cap: KCapture) -> None:
        # This print is simultaneously the new window's Price to Beat and the
        # previous window's resolution price.
        for sess in (self.current, self.next):
            if sess is not None and sess.meta.window_start == cap.window_start:
                sess.set_k(cap.k, cap.oracle_ts_ms)
        self._schedule(
            self.db.k_capture(cap.window_start, cap.k, cap.oracle_ts_ms, cap.recv_wall_ns)
        )
        prev = self.settling.get(cap.window_start)  # window_close == this boundary
        if prev is not None:
            self._settle_session(prev, close_price=cap.k)

    def _on_book_update(self, book: OrderBook, recv_mono_ns: int) -> None:
        self._pump(recv_mono_ns)

    def _on_trade(self, tp: TradePrint) -> None:
        if self.shadow is not None:
            self.shadow.on_trade(tp)
        self._pump(tp.recv_mono_ns)

    def _pump(self, event_mono_ns: int) -> None:
        """Advance shadow time and evaluate. Called on every feed event."""
        if self.shadow is not None:
            self.shadow.process_due(self.clock.mono_ns())
        self.evaluate(event_mono_ns)

    # --------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        """Periodic housekeeping loop for live/shadow-live mode."""
        last_reconcile = 0.0
        last_ntp = -1e12
        while not self._stopping:
            now_s = self.clock.wall_s()
            await self.manage_sessions(now_s)
            if not self.replay_mode and now_s - last_ntp >= NTP_INTERVAL_S:
                last_ntp = now_s
                self._spawn(self._check_ntp())
            if now_s - last_reconcile >= RECONCILE_INTERVAL_S:
                last_reconcile = now_s
                await self._reconcile()
            self._pump(self.clock.mono_ns())
            self._write_status(now_s)
            await asyncio.sleep(TICK_INTERVAL_S)

    async def stop(self) -> None:
        """Graceful shutdown (SIGTERM): cancel-all, log final state (spec §6)."""
        self._stopping = True
        for sess in [s for s in (self.current, self.next) if s is not None]:
            await self.om.cancel_all_for_session(sess.slug)
        for task in self._bg:
            task.cancel()
        log.info("engine stopped", extra={"ctx": {"open_orders": len(self.om.open_orders())}})

    async def manage_sessions(self, now_s: float) -> None:
        """Prefetch next window, roll over at the boundary. The most dangerous
        race in the system — sequenced explicitly here and tested."""
        ws = window_start(now_s, self.cfg.window.length_s)

        # 1. Roll the current session if its window has closed.
        if self.current is not None and now_s >= self.current.meta.window_close:
            await self._close_session(self.current)
            self.current = None

        # 2. Promote next → current at the boundary.
        if self.current is None or self.current.meta.window_start != ws:
            candidate = (
                self.next if (self.next is not None and self.next.meta.window_start == ws) else None
            )
            if candidate is None:
                candidate = await self._create_session(ws)
            if candidate is not None:
                if candidate.state is SessionState.PENDING:
                    candidate.transition(SessionState.ACTIVE)
                self.current = candidate
                self.next = None
                k = self.rtds.get_k(ws)
                if k is not None:
                    candidate.set_k(k.k, k.oracle_ts_ms)

        # 3. Prefetch the next window at T−prefetch (spec §5).
        nxt_ws = ws + self.cfg.window.length_s
        if (
            self.next is None or self.next.meta.window_start != nxt_ws
        ) and nxt_ws - now_s <= self.cfg.window.prefetch_seconds:
            self.next = await self._create_session(nxt_ws)

    async def _create_session(self, ws: int) -> MarketSession | None:
        meta = await self.discovery.resolve(ws)
        if meta is None:
            return None
        sess = MarketSession(
            meta=meta,
            warmup_seconds=self.cfg.risk.warmup_seconds,
            arm_seconds=self.cfg.window.arm_seconds,
            min_seconds=self.cfg.window.min_seconds,
        )
        if meta.fees_known:
            fm = FeeModel(meta)
            self._fee_models[meta.up_token] = fm
            self._fee_models[meta.down_token] = fm
        self.clob.track([meta.up_token, meta.down_token])
        if not self.replay_mode:
            await self.clob.resubscribe()
        k = self.rtds.get_k(ws)
        if k is not None:
            sess.set_k(k.k, k.oracle_ts_ms)
        # Recorded so replays can reconstruct discovery without REST.
        self.decisions.record("market_meta", "engine", asdict(meta))
        log.info(
            "session created", extra={"ctx": {"slug": meta.slug, "fees_known": meta.fees_known}}
        )
        return sess

    async def _close_session(self, sess: MarketSession) -> None:
        """T−0 teardown: cancel-all, reconcile, snapshot, hand to settlement."""
        sess.transition(SessionState.CLOSING)
        await self.om.cancel_all_for_session(sess.slug)
        await self._reconcile()
        for strat in self.strategies:
            strat.on_window_closed(sess)
        sess.transition(SessionState.SETTLING)
        self.settling[sess.meta.window_close] = sess
        # Books for old tokens stay tracked until settlement, then untracked.
        await self._snapshot_window(sess)
        # Late K may already be there (boundary print raced us).
        cap = self.rtds.get_k(sess.meta.window_close)
        if cap is not None:
            self._settle_session(sess, close_price=cap.k)

    def _settle_session(self, sess: MarketSession, close_price: float) -> None:
        if sess.state is not SessionState.SETTLING or sess.k is None:
            if sess.k is None:
                log.error("cannot settle, K missing", extra={"ctx": {"slug": sess.slug}})
                self.settling.pop(sess.meta.window_close, None)
            return
        outcome = sess.settle(close_price)
        pnl = self.ledger.settle_session(sess.meta, outcome)
        self.risk.record_window_result(sess.meta.window_start, pnl)
        self._schedule(
            self.risk.check_daily_loss(self.ledger.daily_realized_pnl(self.clock.wall_s()))
        )
        sess.transition(SessionState.DONE)
        self.settling.pop(sess.meta.window_close, None)
        self.clob.untrack([sess.meta.up_token, sess.meta.down_token])
        self._fee_models.pop(sess.meta.up_token, None)
        self._fee_models.pop(sess.meta.down_token, None)
        self._schedule(self._snapshot_window(sess))
        log.info(
            "session settled",
            extra={"ctx": {"slug": sess.slug, "outcome": outcome, "pnl": round(pnl, 4)}},
        )

    async def _snapshot_window(self, sess: MarketSession) -> None:
        book = self.ledger.sessions.get(sess.slug)
        await self.db.execute(
            "INSERT OR REPLACE INTO windows (window_start, slug, k, k_captured_wall_ns,"
            " outcome, close_price, n_trades, stake_usdc, entry_edge, realized_pnl_usdc,"
            " fees_usdc, fees_shares, mode, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sess.meta.window_start,
                sess.slug,
                sess.k,
                sess.k_oracle_ts_ms * 1_000_000 if sess.k_oracle_ts_ms else None,
                sess.outcome,
                sess.close_oracle_price,
                book.n_fills if book else 0,
                sess.stake_used_usdc,
                sess.entry_edges[0] if sess.entry_edges else None,
                book.realized_pnl_usdc if book else None,
                book.fees_usdc if book else 0.0,
                book.fees_shares if book else 0.0,
                ",".join(
                    sorted(
                        {o.strategy for o in self.om.orders.values() if o.session_slug == sess.slug}
                    )
                )
                or None,
                "; ".join(sess.notes) or None,
            ),
        )

    # --------------------------------------------------------------- decisions
    def evaluate(self, event_mono_ns: int) -> None:
        sess = self.current
        if sess is None or sess.state is not SessionState.ACTIVE:
            return
        now_s = self.clock.wall_s()
        now_mono = self.clock.mono_ns()
        ctx = self._build_context(sess, now_s, now_mono)
        if ctx is None:
            return
        for strat in self.strategies:
            decision = strat.evaluate(ctx)
            if decision.empty:
                self._maybe_log_idle_decision(ctx, now_mono)
                continue
            for client_id in decision.cancel_client_ids:
                order = self.om.orders.get(client_id)
                if order is not None:
                    self._spawn(self.om.request_cancel(order))
            for intent in decision.intents:
                self._handle_intent(strat, intent, ctx, event_mono_ns, now_mono)

    def _build_context(
        self, sess: MarketSession, now_s: float, now_mono: int
    ) -> DecisionContext | None:
        up_book = self.clob.books.get(sess.meta.up_token)
        down_book = self.clob.books.get(sess.meta.down_token)
        if up_book is None or down_book is None:
            return None
        cl = self.rtds.latest
        cl_age_ms = (now_mono - cl.recv_mono_ns) / 1e6 if cl is not None else float("inf")
        bn = self.binance.state
        bn_age_ms = (now_mono - bn.updated_mono_ns) / 1e6 if bn.updated_mono_ns else float("inf")
        fair = None
        if sess.k is not None and self.vol.warmed_up:
            fair = self.fair.evaluate(sess.k, bn.mid, sess.tau(now_s))
        return DecisionContext(
            session=sess,
            now_s=now_s,
            tau_s=sess.tau(now_s),
            phase=sess.phase(now_s),
            k=sess.k,
            s_cl=cl.value if cl is not None else 0.0,
            binance_mid=bn.mid,
            fair=fair,
            up_top=up_book.top(),
            down_top=down_book.top(),
            fee_model=self._fee_models.get(sess.meta.up_token),
            chainlink_age_ms=cl_age_ms,
            binance_age_ms=bn_age_ms,
            book_dirty=up_book.dirty or down_book.dirty,
        )

    def _handle_intent(
        self,
        strat: Strategy,
        intent: OrderIntent,
        ctx: DecisionContext,
        event_mono_ns: int,
        decision_mono_ns: int,
    ) -> None:
        top = ctx.up_top if intent.outcome == "UP" else ctx.down_top
        gate_ctx = GateContext(
            session=ctx.session,
            now_s=ctx.now_s,
            book_top=top,
            book_dirty=ctx.book_dirty,
            chainlink_age_ms=ctx.chainlink_age_ms,
            binance_age_ms=ctx.binance_age_ms,
            ntp_offset_ms=self.ntp_offset_ms,
            in_tie_band=ctx.fair.in_tie_band if ctx.fair is not None else True,
            open_exposure_usdc=self.ledger.open_exposure_usdc(),
            daily_realized_pnl_usdc=self.ledger.daily_realized_pnl(ctx.now_s),
        )
        # Exits are risk-checked lightly: reducing risk must not be blocked by
        # entry gates (tie band, phase, liquidity multiples).
        is_exit = intent.side == "SELL"
        result = self.risk.check_entry(intent, gate_ctx)
        allowed = result.allowed or (
            is_exit and all(g in _EXIT_IGNORABLE_GATES for g in result.failed)
        )
        self._log_decision(ctx, intent, result.failed, allowed)
        if not allowed:
            return
        order = self.om.draft(intent, decision_mono_ns)
        self._order_outcome[order.client_id] = intent.outcome
        if isinstance(strat, ScalperStrategy):
            if intent.side == "BUY":
                strat.register_entry_order(order, intent.outcome)
            else:
                strat.register_exit_order(order)
        ctx.session.stake_used_usdc += intent.price * intent.size if intent.side == "BUY" else 0.0
        ctx.session.entry_edges.append(intent.edge_net)
        ctx.session.open_order_ids.add(order.client_id)
        self._schedule(
            self.db.latency(
                "event_to_decision", (decision_mono_ns - event_mono_ns) / 1e3, self.clock.wall_ns()
            )
        )
        self._spawn(self._submit_and_record(order))

    async def _submit_and_record(self, order: ManagedOrder) -> None:
        ok = await self.om.submit(order)
        for hop, micros in self.om.latency_samples(order).items():
            await self.db.latency(hop, micros, self.clock.wall_ns())
        await self.db.execute(
            "INSERT OR REPLACE INTO orders (order_id, session_slug, token_id, outcome, side,"
            " price, size, tif, strategy, state, filled_size, created_wall_ns, sent_mono_ns,"
            " acked_mono_ns, terminal_mono_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.client_id,
                order.session_slug,
                order.token_id,
                self._order_outcome.get(order.client_id),
                order.side,
                order.price,
                order.size,
                order.tif.value,
                order.strategy,
                order.state.value,
                order.filled_size,
                order.created_wall_ns,
                order.sent_mono_ns,
                order.acked_mono_ns,
                order.terminal_mono_ns,
            ),
        )
        if order.is_terminal and order.side == "BUY":
            for strat in self.strategies:
                if isinstance(strat, ScalperStrategy) and strat.name == order.strategy:
                    strat.note_entry_terminal(order)
        if not ok:
            log.info("submit failed", extra={"ctx": {"id": order.client_id}})

    # ------------------------------------------------------------------- fills
    def _on_shadow_fill(self, fill: Fill) -> None:
        self.om.apply_fill(fill)

    def _on_fill_applied(self, order: ManagedOrder, fill: Fill) -> None:
        sess = self._session_for_slug(order.session_slug)
        ws = sess.meta.window_start if sess is not None else 0
        self.ledger.apply_fill(order.session_slug, ws, fill)
        for strat in self.strategies:
            if strat.name == order.strategy:
                strat.on_fill(order, fill)
        self._schedule(
            self.db.execute(
                "INSERT OR REPLACE INTO fills (trade_id, order_id, token_id, side, price, size,"
                " fee_shares, fee_usdc, exchange_ts_ms, recv_mono_ns) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    fill.trade_id,
                    fill.order_id,
                    fill.token_id,
                    fill.side,
                    fill.price,
                    fill.size,
                    fill.fee_shares,
                    fill.fee_usdc,
                    fill.exchange_ts_ms,
                    fill.recv_mono_ns,
                ),
            )
        )

    def _session_for_slug(self, slug: str) -> MarketSession | None:
        for sess in [self.current, self.next, *self.settling.values()]:
            if sess is not None and sess.slug == slug:
                return sess
        return None

    # ---------------------------------------------------------------- logging
    def _log_decision(
        self,
        ctx: DecisionContext,
        intent: OrderIntent | None,
        failed_gates: list[str],
        allowed: bool,
    ) -> None:
        doc: dict[str, Any] = {
            "slug": ctx.session.slug,
            "now_s": round(ctx.now_s, 3),
            "tau_s": round(ctx.tau_s, 3),
            "phase": ctx.phase.value,
            "K": ctx.k,
            "S_cl": ctx.s_cl,
            "B": ctx.binance_mid,
            "cl_age_ms": round(ctx.chainlink_age_ms, 1),
            "bn_age_ms": round(ctx.binance_age_ms, 1),
            "book_dirty": ctx.book_dirty,
            "up": _top_doc(ctx.up_top),
            "down": _top_doc(ctx.down_top),
        }
        if ctx.fair is not None:
            doc |= {
                "S_hat": round(ctx.fair.s_hat, 2),
                "P_up": round(ctx.fair.p_up, 5),
                "sigma_px_s": round(ctx.fair.sigma_price_per_s, 4),
                "basis_std": round(ctx.fair.basis_std, 4)
                if ctx.fair.basis_std != float("inf")
                else None,
                "tie_band": ctx.fair.in_tie_band,
            }
        if intent is not None:
            doc["action"] = {
                "strategy": intent.strategy,
                "side": intent.side,
                "outcome": intent.outcome,
                "price": intent.price,
                "size": intent.size,
                "tif": intent.tif.value,
                "model_p": round(intent.model_p, 5),
                "edge_net": round(intent.edge_net, 5),
                "reason": intent.reason,
                "gates_failed": failed_gates,
                "allowed": allowed,
            }
        self.decisions.record("decision", "engine", doc)
        self._last_decision_log_ns = self.clock.mono_ns()

    def _maybe_log_idle_decision(self, ctx: DecisionContext, now_mono: int) -> None:
        interval = 0 if ctx.phase is Phase.ARMED else DECISION_LOG_IDLE_INTERVAL_NS
        if now_mono - self._last_decision_log_ns >= interval:
            self._log_decision(ctx, None, [], False)

    # ------------------------------------------------------------ housekeeping
    async def _check_ntp(self) -> None:
        try:
            sample = await ntp_offset(self.cfg.run.ntp_server)
            self.ntp_offset_ms = sample.offset_s * 1000.0
        except (OSError, ValueError, TimeoutError) as exc:
            self.ntp_offset_ms = None  # unknown → clock gate blocks trading
            log.warning("ntp check failed", extra={"ctx": {"err": repr(exc)}})

    async def _reconcile(self) -> None:
        """Reconcile open orders/balances vs the exchange (spec §3).

        Live adapter: REST /data/orders + /data/trades diff. Shadow: resting
        orders are authoritative in-process, so only invariants are checked.
        """
        stale = [
            o
            for o in self.om.open_orders()
            if o.state in (OrderState.SENT,)
            and (self.clock.mono_ns() - o.sent_mono_ns) > 30 * 10**9
        ]
        for o in stale:
            log.error("order stuck in SENT>30s", extra={"ctx": {"id": o.client_id}})
            await self.db.incident("engine", "stuck_order", o.client_id, self.clock.wall_ns())

    async def _on_kill(self, reason: str) -> None:
        for sess in [s for s in (self.current, self.next) if s is not None]:
            await self.om.cancel_all_for_session(sess.slug)
        await self.db.incident("engine", "kill", reason, self.clock.wall_ns())
        self._stopping = True

    def _write_status(self, now_s: float) -> None:
        """Atomic status.json for the dashboard (read-only consumer)."""
        sess = self.current
        doc = {
            "ts": now_s,
            "killed": self.risk.killed,
            "kill_reason": self.risk.kill_reason,
            "ntp_offset_ms": self.ntp_offset_ms,
            "binance_age_ms": self.binance.health.age_ms,
            "chainlink_age_ms": self.rtds.health.age_ms,
            "clob_age_ms": self.clob.health.age_ms,
            "open_orders": len(self.om.open_orders()),
            "open_exposure_usdc": self.ledger.open_exposure_usdc(),
            "daily_pnl_usdc": self.ledger.daily_realized_pnl(now_s),
            "session": None,
        }
        if sess is not None:
            doc["session"] = {
                "slug": sess.slug,
                "phase": sess.phase(now_s).value,
                "K": sess.k,
                "tau_s": round(sess.tau(now_s), 1),
                "state": sess.state.value,
            }
        try:
            path = Path(self.cfg.run.status_file)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(orjson.dumps(doc))
            tmp.replace(path)
        except OSError:
            pass  # status file is best-effort; never let it break trading

    # ------------------------------------------------------------------ utils
    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        task.add_done_callback(_log_task_exception)
        self._bg.append(task)
        self._bg = [t for t in self._bg if not t.done()]

    def _schedule(self, coro: Any) -> None:
        self._spawn(coro)


_EXIT_IGNORABLE_GATES = {
    "tie_band",
    "thin_book",
    "window_stake_cap",
    "exposure_cap",
    "daily_loss_budget",
    "below_min_size",
} | {f"phase:{p.value}" for p in Phase}


def _top_doc(top: BookTop) -> dict[str, Any]:
    return {
        "bid": top.bid,
        "bid_sz": top.bid_size,
        "ask": top.ask,
        "ask_sz": top.ask_size,
    }


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task failed", extra={"ctx": {"err": repr(exc)}})
