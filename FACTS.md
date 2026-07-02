# FACTS.md — Verified exchange facts

Every external assumption the bot relies on, with source and verification date.

**Verification statuses:**

- `VERIFIED-SOURCE` — read directly from an official Polymarket artifact (official client
  source code from PyPI/npm, or official docs content).
- `VERIFIED-DOCS` — confirmed against docs.polymarket.com content surfaced via search index
  and corroborated by ≥2 independent secondary sources. Treat as reliable but re-check live.
- `UNVERIFIED-RUNTIME` — could not be confirmed from the build environment (Polymarket API
  hosts are egress-blocked here). **Must** be verified with `scripts/verify_facts.py` from
  the deployment VPS before M1 sign-off. The bot also re-verifies the runtime-critical
  subset programmatically at startup and refuses to trade on mismatch.

> Build-environment note (2026-07-01): direct HTTPS to `*.polymarket.com` is denied by this
> sandbox's egress policy (Cloudflare + org proxy). Facts below were verified from official
> package registries (PyPI `py-clob-client` 0.34.6, npm `@polymarket/real-time-data-client`
> 1.4.0 — both allowlisted) and search-indexed official docs. `scripts/verify_facts.py`
> performs the full live re-verification and writes `facts_runtime.json`.

---

## 1. Market structure

| # | Fact | Status | Source | Date |
|---|------|--------|--------|------|
| 1.1 | 5-minute "Bitcoin Up or Down" markets exist and list continuously | VERIFIED-DOCS | Live event pages `polymarket.com/event/btc-updown-5m-1777256700` et al.; [CoinMarketCap launch coverage](https://coinmarketcap.com/academy/article/polymarket-debuts-5-minute-bitcoin-prediction-markets-with-instant-settlement); [Polymarket on X](https://x.com/Polymarket/status/2022343906168623112) | 2026-07-01 |
| 1.2 | Event slug is deterministic: `btc-updown-5m-{window_start_unix}` with `window_start_unix % 300 == 0` | VERIFIED-DOCS | Live event URLs above (timestamps 1777256700, 1777638000, 1771562700 — all ≡ 0 mod 300); Medium: "Unlocking Edges in Polymarket's 5-Minute Crypto Markets" | 2026-07-01 |
| 1.3 | Resolution: **UP if Chainlink BTC/USD at window close ≥ price at window open, else DOWN. Tie resolves UP.** Resolution source is the Chainlink BTC/USD **data stream**, not Binance or any spot exchange | VERIFIED-DOCS | Market rules text quoted in launch coverage + market pages ("greater than or equal to") | 2026-07-01 |
| 1.4 | "Price to Beat" = first Chainlink data-stream print at/after the window boundary; shown live on the market page | VERIFIED-DOCS | Market page behavior described in multiple sources; **exact boundary semantics (first print at/after vs interpolation) → UNVERIFIED-RUNTIME, validate in M1 by comparing our captured K vs the site's displayed Price to Beat over ≥ 100 windows** | 2026-07-01 |
| 1.5 | Two outcome tokens per market (UP / DOWN), CTF ERC-1155, redeem $1.00 / $0.00 | VERIFIED-DOCS | [Chainstack: Polymarket API for developers](https://chainstack.com/polymarket-api-for-developers/); standard CTF mechanics | 2026-07-01 |
| 1.6 | Gamma market object fields used: `clobTokenIds`, `conditionId`, `negRisk`, `orderPriceMinTickSize`, `orderMinSize`, `endDate`, `slug` | UNVERIFIED-RUNTIME | Field names from Gamma docs mirrors; exact shape for 5m markets must be captured live | — |
| 1.7 | 5m markets are **not** neg-risk (plain binary) | UNVERIFIED-RUNTIME | Assumed from binary structure; check `/neg-risk` per token | — |

## 2. Fees

| # | Fact | Status | Source | Date |
|---|------|--------|--------|------|
| 2.1 | Dynamic taker fee formula: `fee = shares × rate × price × (1 − price)` — bell curve peaking at 50¢, ~0 at extremes | VERIFIED-DOCS | [startpolymarket.com fee table](https://startpolymarket.com/learn/polymarket-fees/); [Market Math fee guide (March 2026)](https://marketmath.io/blog/polymarket-fees-explained); [KuCoin 2026 guide](https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026) | 2026-07-01 |
| 2.2 | Crypto category peak ≈ **$1.80 per 100 shares** at 50¢ ⇒ effective rate ≈ 0.072 (720 bps on p(1−p)). Schedule changed several times in 2026 (Jan/Mar/Apr) | VERIFIED-DOCS (exact current rate UNVERIFIED-RUNTIME) | Same as 2.1. **Never hardcode: query per-token via CLOB `GET /fee-rate`** | 2026-07-01 |
| 2.3 | CLOB exposes per-token fee rate: `GET /fee-rate` → `{"base_fee": <bps>}`; the official client injects it into the signed order as `feeRateBps` and rejects user-supplied rates that mismatch the market's rate | VERIFIED-SOURCE | `py-clob-client` 0.34.6: `endpoints.py` (`GET_FEE_RATE = "/fee-rate"`), `client.py:450-490` (`get_fee_rate_bps`, `__resolve_fee_rate`) | 2026-07-01 |
| 2.4 | Fees are charged **on proceeds** (output asset): BUY → fee taken in **shares**, SELL → fee taken in **collateral**. Accounting must reconcile from actual fill events | VERIFIED-SOURCE (mechanism) / UNVERIFIED-RUNTIME (exact fill-event fields) | `py-clob-client` `OrderArgs.fee_rate_bps` docstring: "charged on proceeds"; CTF Exchange contract semantics; corroborated by fee guides ("effective acquisition price p + f·p(1−p)") | 2026-07-01 |
| 2.5 | Makers pay zero; taker fees fund a daily maker-rebate program, crypto rebate share ≈ 20% | VERIFIED-DOCS | [Polymarket Help Center: Maker Rebates Program](https://help.polymarket.com/en/articles/13364471-maker-rebates-program); [DeFi Rate coverage](https://defirate.com/news/polymarket-users-approve-new-crypto-market-fees/) | 2026-07-01 |
| 2.6 | Fees were introduced specifically to blunt latency arbitrage on short-duration crypto markets | VERIFIED-DOCS | [Finance Magnates: "Polymarket Introduces Dynamic Fees to Curb Latency Arbitrage"](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/) | 2026-07-01 |

## 3. CLOB REST API

| # | Fact | Status | Source | Date |
|---|------|--------|--------|------|
| 3.1 | Base URL `https://clob.polymarket.com`, Polygon chain id 137 | VERIFIED-SOURCE | `py-clob-client` 0.34.6 `constants.py` (`POLYGON = 137`), README | 2026-07-01 |
| 3.2 | Endpoints: `/book`, `/books`, `/price`, `/midpoint`, `/spread`, `/last-trade-price`, `POST /order`, `POST /orders`, `DELETE /order`, `DELETE /orders`, `DELETE /cancel-all`, `DELETE /cancel-market-orders`, `/data/orders`, `/data/trades`, `/tick-size`, `/neg-risk`, `/fee-rate`, `/balance-allowance`, `/time`, `POST /v1/heartbeats`, `/sampling-markets`, `/markets` | VERIFIED-SOURCE | `py-clob-client` 0.34.6 `endpoints.py` | 2026-07-01 |
| 3.3 | Order types (time-in-force): **GTC, FOK, GTD, FAK** — FAK = fill-and-kill = IOC-equivalent for partial-fill-capable taker orders; FOK = all-or-nothing | VERIFIED-SOURCE | `py-clob-client` 0.34.6 `clob_types.py` `class OrderType` | 2026-07-01 |
| 3.4 | Auth levels L0/L1/L2. L1 = private key (EIP-712), L2 = API key/secret/passphrase (HMAC headers), derived via `/auth/derive-api-key` / `/auth/api-key`. Read-only API keys also exist | VERIFIED-SOURCE | `py-clob-client` 0.34.6 `constants.py`, `endpoints.py`, `headers/headers.py` | 2026-07-01 |
| 3.5 | **Price and size are baked into the EIP-712 signature** (`makerAmount`/`takerAmount` in the signed struct). A signed order's price cannot be mutated ⇒ pre-signing requires a **ladder of discrete (price, size) candidates** | VERIFIED-SOURCE | `py-clob-client` 0.34.6 `order_builder/builder.py` (`get_order_amounts` → signed `OrderData`) | 2026-07-01 |
| 3.6 | Tick sizes are one of {0.1, 0.01, 0.001, 0.0001}; price/size rounding rules per tick size per `ROUNDING_CONFIG`; token amounts are 6-decimals fixed point | VERIFIED-SOURCE | `py-clob-client` 0.34.6 `order_builder/builder.py`, `helpers.py` | 2026-07-01 |
| 3.7 | Tick size **changes dynamically** when book price crosses 0.96 / 0.04 (emits `tick_size_change` on WS) | VERIFIED-DOCS | docs.polymarket.com Market Channel page (search-indexed) | 2026-07-01 |
| 3.8 | Rate limits (2026): general CLOB ≈ 9,000 req/10s; `POST /order` ≈ 3,500/10s burst and 36,000/10min sustained; enforced via Cloudflare throttling (delayed, not rejected); sliding windows | VERIFIED-DOCS (numbers UNVERIFIED-RUNTIME) | [docs.polymarket.com/api-reference/rate-limits](https://docs.polymarket.com/api-reference/rate-limits) (search-indexed); [AgentBets rate-limit guide (March 2026)](https://agentbets.ai/guides/polymarket-rate-limits-guide/) | 2026-07-01 |
| 3.9 | Collateral: **USDC.e on Polygon**; allowances needed for CTF Exchange contracts. PUSD references in 2026 retail flows **do not** change API-trader settlement | VERIFIED-DOCS (USDC.e) / UNVERIFIED-RUNTIME (PUSD status) | Chainstack guide; `py-clob-client` collateral handling; **confirm via `/balance-allowance` from VPS** | 2026-07-01 |
| 3.10 | Heartbeat endpoint `POST /v1/heartbeats` exists (keep-alive / cancel-on-disconnect semantics) | VERIFIED-SOURCE (existence) / UNVERIFIED-RUNTIME (semantics) | `py-clob-client` 0.34.6 `endpoints.py` | 2026-07-01 |

## 4. CLOB WebSocket

| # | Fact | Status | Source | Date |
|---|------|--------|--------|------|
| 4.1 | Base `wss://ws-subscriptions-clob.polymarket.com`; market channel at `/ws/market`, user channel at `/ws/user` | VERIFIED-DOCS | docs.polymarket.com WSS overview + Market Channel (search-indexed); multiple client repos | 2026-07-01 |
| 4.2 | Market channel subscribe: `{"assets_ids": [...tokenIds], "type": "market"}`; optional `custom_feature_enabled: true` for extra event types | VERIFIED-DOCS | docs Market Channel page | 2026-07-01 |
| 4.3 | Market channel events: `book` (full snapshot, sent on subscribe and on trade-affecting events), `price_change` (level deltas; size "0" ⇒ level removed), `tick_size_change`, `last_trade_price` | VERIFIED-DOCS | docs Market Channel page (search-indexed) | 2026-07-01 |
| 4.4 | Client must send `PING` every ~10 s; server replies `PONG` | VERIFIED-DOCS | docs WSS overview | 2026-07-01 |
| 4.5 | User channel requires L2 API creds in subscribe; delivers `order` (lifecycle) and `trade` (fill) events | VERIFIED-DOCS | docs User Channel; RTDS `clob_user` schema in official npm client README | 2026-07-01 |
| 4.6 | `book` messages carry a `hash` field for integrity/sequencing checks | UNVERIFIED-RUNTIME | Docs mirrors mention hash; exact gap-detection semantics must be captured live in M1 | — |

## 5. RTDS (real-time data service)

| # | Fact | Status | Source | Date |
|---|------|--------|--------|------|
| 5.1 | Endpoint `wss://ws-live-data.polymarket.com` | VERIFIED-SOURCE | npm `@polymarket/real-time-data-client` 1.4.0 `dist/client.js`: `DEFAULT_HOST = "wss://ws-live-data.polymarket.com"` | 2026-07-01 |
| 5.2 | Topic **`crypto_prices_chainlink`**, filter `{"symbol":"btc/usd"}` (slash-separated), no auth. Message: `{"topic":"crypto_prices_chainlink","type":"update","timestamp":<ms>,"payload":{"symbol":"btc/usd","timestamp":<ms>,"value":<float>}}` | VERIFIED-DOCS | [docs.polymarket.com/market-data/websocket/rtds](https://docs.polymarket.com/market-data/websocket/rtds) (search-indexed, message sample quoted) | 2026-07-01 |
| 5.3 | Subscribe frame: `{"action":"subscribe","subscriptions":[{"topic":...,"type":"*","filters":"<json-string>"}]}`; `unsubscribe` symmetric; PING every ~5 s | VERIFIED-SOURCE (frame shape from npm client) + VERIFIED-DOCS (5s ping) | npm client 1.4.0 source + docs RTDS page | 2026-07-01 |
| 5.4 | Older `crypto_prices` topic (Binance-sourced, `{"symbol":"btcusdt"}` filter) also exists — **not** the resolution series; do not confuse the two | VERIFIED-SOURCE | npm client 1.4.0 README topics table | 2026-07-01 |
| 5.5 | RTDS also carries `clob_market` (agg_orderbook/price_change/last_trade_price/tick_size_change) and `clob_user` topics — possible backup path for CLOB data | VERIFIED-SOURCE | npm client 1.4.0 README | 2026-07-01 |
| 5.6 | Whether the RTDS Chainlink series ticks frequently enough near boundaries, and its exact print cadence, is **measured**, not assumed | UNVERIFIED-RUNTIME | M1 data-quality report requirement | — |

## 6. Binance

| # | Fact | Status | Source | Date |
|---|------|--------|--------|------|
| 6.1 | Spot combined stream: `wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/btcusdt@aggTrade`; futures alt: `wss://fstream.binance.com/stream?...` | VERIFIED-DOCS (stable, long-documented API) | Binance spot WS docs (public, stable since 2019); re-verify handshake at runtime | 2026-07-01 |
| 6.2 | `bookTicker` payload: `u,s,b,B,a,A` (update id, symbol, best bid px/qty, best ask px/qty); `aggTrade`: `p,q,T,m…`; combined-stream wrapper `{"stream":...,"data":{...}}` | VERIFIED-DOCS | Binance WS docs | 2026-07-01 |
| 6.3 | Binance leads Chainlink for BTC (Chainlink aggregates from CEX feeds incl. Binance with publication latency). Lead/lag λ is **calibrated from recorded data**, not assumed | UNVERIFIED-RUNTIME (magnitude) | M1/M2 calibration requirement | — |

## 7. Access / infrastructure

| # | Fact | Status | Source | Date |
|---|------|--------|--------|------|
| 7.1 | Polymarket geo-blocks some jurisdictions and fronts with Cloudflare; run from an allowed region (EU VPS, ref. GCP `europe-west1`) | VERIFIED-DOCS | Polymarket ToS/geo docs (widely documented); this sandbox is itself blocked (403 CONNECT) | 2026-07-01 |
| 7.2 | `py-clob-client` (PyPI, v0.34.6 as of 2026-07-01) is the official Python client; signing via `py-order-utils` | VERIFIED-SOURCE | PyPI | 2026-07-01 |

---

## Runtime re-verification

`scripts/verify_facts.py` (run from the deployment VPS) checks, live:

1. Gamma: fetch event/market for the current `btc-updown-5m-{ws}` slug; dump full JSON; confirm 1.2, 1.6, 1.7.
2. CLOB REST: `/time`, `/markets/{condition_id}`, `/tick-size`, `/neg-risk`, `/fee-rate` per token; confirm 2.2, 2.3, 3.x.
3. CLOB WS: connect `/ws/market`, subscribe both tokens, capture `book` + `price_change` + `tick_size_change` samples; confirm 4.x schemas (writes samples to `facts_runtime.json`).
4. RTDS: subscribe `crypto_prices_chainlink` `btc/usd`; capture prints across one 300 s boundary; confirm 5.2/5.6 and boundary semantics vs the site's Price to Beat (1.4).
5. Binance: connect combined stream, one message per stream type; confirm 6.1/6.2.
6. Balance/allowance: `/balance-allowance` — confirm collateral token (3.9).

The engine additionally re-validates the runtime-critical subset (fee rate, tick size, min size, neg-risk, token ids) at every market discovery and **refuses to arm** if any check fails or is stale.
