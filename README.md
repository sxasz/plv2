# polymkt-bot — Polymarket 5-minute BTC Up/Down latency bot

Production-grade automated trading system for Polymarket's 5-minute
"Bitcoin Up or Down" binary markets, exploiting the repricing lag between the
fast underlying BTC price (Binance) and Polymarket's CLOB, priced against the
**Chainlink** oracle series the markets actually resolve on.

**Status: M0 complete (facts + scaffolding + full shadow stack). No live
trading until M2's Edge Realization Report is GO — the live adapter is
interlocked (see below).**

## Architecture

```
Binance WS ──────┐                      ┌─▶ Mode A sniper   (late-window IOC)
Chainlink RTDS ──┼─▶ FairValueModel ────┼─▶ Mode B scalper  (impulse lag, off)
CLOB market WS ──┘   P_up, edge, fees   └─▶ Mode C maker    (scaffold, off)
      │                                            │ intents
      ▼                                            ▼
  Recorder (raw JSONL, ns)              RiskManager (hard gates, kill switch)
      │                                            │ approved
      ▼                                            ▼
  Replayer ──▶ identical engine ──▶ OrderManager FSM ──▶ ExchangeAdapter
                                    DRAFT→SIGNED→SENT→ACKED→PARTIAL*→terminal
                                          │                 ├─ ShadowExchange (M2)
                                    SQLite + decisions      └─ LiveClobExchange (M3)
```

Key design rules (see the spec, enforced by tests):

- **WebSocket-first**: REST never serves prices on the hot path.
- **An ACK is not a fill**; fills come only from the user channel / trade
  reconciliation. Cancel races are handled on both sides.
- **Fees are never hardcoded**: per-token rate from CLOB `/fee-rate`;
  markets with unknown fees are untradeable.
- **Honest shadow fills**: RTT-delayed, depth-limited takers; tape-confirmed
  last-in-queue makers; no marking at mid; no lookahead.
- **Window isolation**: each 5-minute window is a `MarketSession`; teardown at
  T−0 is explicit and late fills route to the owning session.

## Quick start

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # + ".[live]" only for M3
cp .env.example .env             # fill in credentials (never committed)

# M0: verify every exchange fact live (run from the EU VPS):
python scripts/verify_facts.py

# M1: record 24h of all three feeds:
python -m polymkt_bot.main --mode record

# M2: shadow-live (identical code path, simulated fills):
python -m polymkt_bot.main --mode shadow

# Replay a recording deterministically:
python -m polymkt_bot.main --mode replay --raw-dir data/raw

# Dashboard (read-only, localhost):
python -m polymkt_bot.dashboard.app
```

## Live trading interlock

`--mode live` refuses to start unless **all** of:

1. `run.shadow_mode: false` in `config.yaml`,
2. `ENABLE_LIVE_TRADING=YES_I_ACCEPT_REAL_LOSSES` in `.env`,
3. valid `POLYMARKET_*` credentials.

Per the milestone plan, do not set these until the M2 GO/NO-GO gate passes
(executable PnL net of fees positive, p < 0.05 bootstrap, sane calibration).

## Verified facts

Every external assumption lives in [FACTS.md](FACTS.md) with source, date and
verification status. `scripts/verify_facts.py` re-verifies the
runtime-critical subset live and writes `facts_runtime.json`; the engine
re-checks fee/tick/neg-risk params at every market discovery and refuses to
arm on mismatch.

## Development

```bash
ruff check . && ruff format --check . && mypy polymkt_bot && pytest
```

CI runs the same on every push. `data/`, `.env` and all recordings are
git-ignored.

## Milestones

- **M0** ✅ facts & scaffolding (this commit)
- **M1** recorders 24h unattended + data-quality report (K-capture accuracy
  vs the site's Price to Beat, gaps, reconnects, basis stats)
- **M2** shadow ≥ 500 windows → Edge Realization Report → GO/NO-GO
- **M3** live micro-stakes, 3 days incident-free, slippage vs shadow
- **M4** pre-signed order ladders, λ calibration, maker module tuning
