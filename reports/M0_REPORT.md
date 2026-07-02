# M0 Report — Facts & Scaffolding

Date: 2026-07-01 · Branch: `claude/polymarket-btc-latency-bot-tz0a2q` · Status: **complete, awaiting sign-off**

## 1. What was delivered

### Fact verification (`FACTS.md`)

Every [VERIFY] item from the spec was researched and recorded with source + date.
Highest-confidence findings came from **official Polymarket client source code**
pulled from package registries (PyPI `py-clob-client` 0.34.6, npm
`@polymarket/real-time-data-client` 1.4.0):

- Order types are **GTC / FOK / GTD / FAK** — FAK is the IOC equivalent. (3.3)
- **Price and size are baked into the EIP-712 signature** (`makerAmount`/`takerAmount`),
  so M4's pre-signing optimization must build a ladder of discrete price/size
  candidates. (3.5)
- CLOB exposes `/fee-rate` (returns `base_fee` in bps) per token; the official client
  injects it into the signed order and rejects mismatched user-supplied rates. (2.3)
- Tick sizes ∈ {0.1, 0.01, 0.001, 0.0001}, with per-tick rounding rules. (3.6)
- RTDS endpoint `wss://ws-live-data.polymarket.com`, topic **`crypto_prices_chainlink`**,
  filter `{"symbol":"btc/usd"}`, PING every 5 s. (5.1–5.3)

Confirmed via docs/search corroboration: fee formula `rate × p × (1−p)` peaking
≈ $1.80/100 shares for crypto; maker fees zero with ~20% rebate share; slug format
`btc-updown-5m-{ws}` (live event URLs confirm); tie resolves UP on the Chainlink
BTC/USD data stream; CLOB WS `/ws/market` + `/ws/user` with book/price_change/
tick_size_change/last_trade_price events; rate limits (~3,500/10s burst on POST
/order); collateral USDC.e on Polygon 137.

### Environment constraint & mitigation

This build environment's egress policy blocks `*.polymarket.com` (Cloudflare 403 at
the proxy), so live API responses could not be captured here. Mitigations shipped:

1. **`scripts/verify_facts.py`** — runs all six live check groups from the VPS
   (Gamma slug → market object; CLOB REST params; CLOB WS samples; RTDS boundary
   capture across a 300 s window; Binance streams; fee-formula sanity) and writes
   `facts_runtime.json`. Exit code gates CI/deploy.
2. The engine **re-validates fee/tick/neg-risk at every market discovery** and marks
   markets with unknown params untradeable (`fees_known=False` → hard risk gate).
3. Items that only live traffic can settle are explicitly tagged
   `UNVERIFIED-RUNTIME` in FACTS.md (exact WS event schemas #4.6, fee-in-shares
   accounting fields #2.4, PUSD status #3.9, RTDS print cadence #5.6). Raw-frame
   recording (M1) is designed to close them.

### Scaffolding (full repo skeleton per spec §3)

- **Feeds**: Binance combined stream, RTDS Chainlink (K capture keyed on oracle
  timestamps, idempotent, backfillable from reconnect dumps), CLOB market book
  mirror (pessimistic dirty-marking, REST resync, trade tape), CLOB user channel.
  Common base: jittered backoff reconnect, ping, staleness watchdog, raw-frame
  recording, no silent resyncs.
- **Model**: EWMA σ (irregular sampling), Binance↔Chainlink basis tracker (blocks
  trading until ≥8 samples), `P_up = Φ((Ŝ−K)/(σ√τ + basis_std))` with tie bonus
  and clipping; fee-aware edge.
- **Execution**: order FSM exactly as specified (ACK ≠ fill; cancel races handled
  both directions; fills after confirmed cancel hit accounting and raise
  incidents); live adapter written but triple-interlocked (config + .env
  acknowledgement string + credentials) and off until M2 GO.
- **Risk**: all §6 gates, kill switch, cooldowns, NTP guard (dependency-free SNTP).
- **Strategies**: Mode A sniper (armed window, one-shot, fee-aware edge, depth-capped
  FAK), Mode B scalper (impulse + stale-book trigger, maker-first exit, cross-out
  with explicit round-trip fee math, hold conversion; **disabled by default**),
  Mode C maker scaffold (disabled; no post-only exists on Polymarket — noted).
- **Shadow exchange**: RTT-delayed (measured-RTT reservoir preferred), visible-depth-
  limited taker fills, FOK semantics, tape-confirmed last-in-queue maker fills, buy
  fees in shares, dirty-book kills, seeded determinism.
- **Replayer**: strictly receive-time ordered, monotonic-clamped, feeds frames
  through the *identical* live parsers into the *identical* engine; virtual clock;
  recorded `market_meta` reconstitutes discovery offline.
- **Storage**: bounded-queue JSONL recorder (drops counted, never blocks the loop),
  SQLite summaries (windows, orders, fills, latency hops, incidents, K captures).
- **Dashboard**: read-only Flask on localhost reading SQLite + atomic status.json.
- **Ops**: `main.py` modes record/shadow/replay/live; SIGTERM graceful shutdown;
  uvloop optional; GitHub Actions CI (ruff + ruff format + mypy strict + pytest).

## 2. Test evidence (89 tests, all passing)

Spec §10's mandatory list is covered:

| Requirement | Tests |
|---|---|
| FSM incl. cancel race + post-cancel fill | `test_order_fsm.py` (8) |
| Window rollover with in-flight orders | `test_rollover.py` (3) + `test_replay_end_to_end.py::test_no_leakage_across_windows` |
| Book resync on gap/anomaly | `test_book.py` (8) |
| Fee math vs published examples | `test_fees.py` (5) — $1.80/100 @ 50¢ reproduced |
| Tie-band + staleness gates | `test_gates.py` (14) |
| Replayer determinism | `test_replay_end_to_end.py::test_replay_is_deterministic_byte_identical` (byte-identical decision stream over 2 runs) + `test_replayer.py` (3) |
| Honest shadow fills | `test_shadow.py` (9) — RTT delay, depth limits, FOK, fee-in-shares, conservative maker queue, no post-only |
| End-to-end | synthetic 5.5-min recording: discovery → K capture → vol warmup → arm → snipe → honest fill → settlement with correct PnL |

`ruff check`, `ruff format --check`, `mypy --strict` (41 files): clean.

## 3. Known gaps / decisions needing your input (M0 exit questions)

1. **VPS + credentials** (blocks M1): need the EU VPS provisioned and, for M3 only,
   a funded wallet + L2 API creds. M1 recording needs **no credentials at all**.
2. **WS event schemas** (#4.6): book `hash` validation semantics are recorded but
   not enforced yet — M1's captured frames will fix the exact algorithm; until
   then the mirror treats anomalies pessimistically (dirty → no trading).
3. **Fee denomination on buys** (#2.4): shadow charges buy fees in shares
   (proceeds convention). If M1/M3 fill events show USD-denominated fees, flip
   `ShadowExchange.fee_buy_in_shares` — accounting already reads only fill events.
4. **Reconciliation via REST** (spec §3, every 30 s) is implemented as invariant
   checks in shadow; the live REST diff (`/data/orders`, `/data/trades`) lands
   with the M3 adapter work since it needs L2 auth.

## 4. Proposed next step (M1)

Deploy to the EU VPS, run `scripts/verify_facts.py` (expect `all_ok: true`,
attach `facts_runtime.json` here), then `--mode record` for 24 h unattended and
deliver the data-quality report: feed gaps/reconnects, RTDS print cadence,
Binance↔Chainlink basis stats, and K-capture accuracy vs the site's displayed
Price to Beat.
