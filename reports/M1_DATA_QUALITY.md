# M1 Data-Quality Report — 5-minute BTC Up/Down recorder

Generated 2026-07-02 11:03:57 UTC · branch `claude/polymarket-btc-latency-bot-tz0a2q`

**Recording analyzed: 2026-07-02 05:57:48 UTC → 2026-07-02 10:59:49 UTC (5.03 h).** Note this is **less than the 24 h intended** for M1: the recorder first came up at 05:57:48Z and was restarted at 06:40:21Z to deploy the live-API fixes (commit `3d645c5`). Everything below therefore splits pre-fix vs post-fix where it matters, and the verdict accounts for the short sample.

**Raw archive inventory** (`/opt/plv2/data/raw/`):

| file | size (MB) | frames | first frame | last frame |
|---|---|---|---|---|
| 1782968400.jsonl.gz | 4 | 56,537 | 2026-07-02 05:57:48.938Z | 2026-07-02 05:59:59.997Z |
| 1782972000.jsonl.gz | 147 | 1,869,765 | 2026-07-02 06:00:00.000Z | 2026-07-02 06:59:59.998Z |
| 1782975600.jsonl.gz | 213 | 2,547,935 | 2026-07-02 07:00:00.000Z | 2026-07-02 07:59:59.999Z |
| 1782979200.jsonl | 1,790 | 2,270,091 | 2026-07-02 08:00:00.002Z | 2026-07-02 08:59:59.998Z |
| 1782982800.jsonl | 1,774 | 2,404,373 | 2026-07-02 09:00:00.000Z | 2026-07-02 09:59:59.998Z |
| 1782986400.jsonl | 1,758 | 2,271,434 | 2026-07-02 10:00:00.004Z | 2026-07-02 10:59:49.236Z |

## 1. Feed health

| feed | frames | span (h) | avg fps | gaps > 1s | gaps > threshold | threshold |
|---|---|---|---|---|---|---|
| binance | 2,461,739 | 5.03 | 135.9 | 134 | **2** | 2s |
| rtds_chainlink | 17,577 | 5.03 | 1.0 | 9,674 | **13** | 5s |
| clob_market | 8,940,819 | 5.03 | 493.4 | 189 | **2** | 10s |

**binance — 10 largest inter-frame gaps** (receive-time, UTC):

| gap (s) | from | to | note |
|---|---|---|---|
| 2.93 | 2026-07-02 06:22:39.394Z | 2026-07-02 06:22:42.327Z |  |
| 2.17 | 2026-07-02 06:17:26.105Z | 2026-07-02 06:17:28.274Z |  |
| 1.91 | 2026-07-02 06:23:24.429Z | 2026-07-02 06:23:26.336Z |  |
| 1.89 | 2026-07-02 08:52:31.883Z | 2026-07-02 08:52:33.774Z |  |
| 1.88 | 2026-07-02 06:43:40.277Z | 2026-07-02 06:43:42.154Z |  |
| 1.84 | 2026-07-02 06:45:35.855Z | 2026-07-02 06:45:37.692Z |  |
| 1.82 | 2026-07-02 08:08:57.537Z | 2026-07-02 08:08:59.359Z |  |
| 1.79 | 2026-07-02 06:13:34.284Z | 2026-07-02 06:13:36.075Z |  |
| 1.75 | 2026-07-02 08:55:33.464Z | 2026-07-02 08:55:35.209Z |  |
| 1.68 | 2026-07-02 08:13:25.003Z | 2026-07-02 08:13:26.679Z |  |

**rtds_chainlink — 10 largest inter-frame gaps** (receive-time, UTC):

| gap (s) | from | to | note |
|---|---|---|---|
| 8.87 | 2026-07-02 08:40:44.319Z | 2026-07-02 08:40:53.191Z |  |
| 8.77 | 2026-07-02 06:34:38.469Z | 2026-07-02 06:34:47.240Z |  |
| 8.74 | 2026-07-02 08:15:52.268Z | 2026-07-02 08:16:01.004Z |  |
| 8.62 | 2026-07-02 06:57:58.320Z | 2026-07-02 06:58:06.941Z |  |
| 8.57 | 2026-07-02 07:05:09.165Z | 2026-07-02 07:05:17.734Z |  |
| 8.45 | 2026-07-02 06:01:18.386Z | 2026-07-02 06:01:26.833Z |  |
| 8.35 | 2026-07-02 10:39:11.217Z | 2026-07-02 10:39:19.565Z |  |
| 8.07 | 2026-07-02 09:23:12.416Z | 2026-07-02 09:23:20.488Z |  |
| 7.73 | 2026-07-02 06:41:42.659Z | 2026-07-02 06:41:50.385Z |  |
| 7.48 | 2026-07-02 10:20:22.381Z | 2026-07-02 10:20:29.858Z |  |

**clob_market — 10 largest inter-frame gaps** (receive-time, UTC):

| gap (s) | from | to | note |
|---|---|---|---|
| 10.00 | 2026-07-02 06:36:03.182Z | 2026-07-02 06:36:13.183Z |  |
| 10.00 | 2026-07-02 06:06:19.743Z | 2026-07-02 06:06:29.744Z |  |
| 10.00 | 2026-07-02 06:21:37.283Z | 2026-07-02 06:21:47.281Z |  |
| 10.00 | 2026-07-02 06:27:05.471Z | 2026-07-02 06:27:15.467Z |  |
| 9.27 | 2026-07-02 06:06:30.474Z | 2026-07-02 06:06:39.744Z |  |
| 9.16 | 2026-07-02 06:06:40.580Z | 2026-07-02 06:06:49.745Z |  |
| 8.68 | 2026-07-02 06:26:45.466Z | 2026-07-02 06:26:54.145Z |  |
| 8.58 | 2026-07-02 06:01:30.321Z | 2026-07-02 06:01:38.905Z |  |
| 8.44 | 2026-07-02 06:16:01.026Z | 2026-07-02 06:16:09.464Z |  |
| 8.12 | 2026-07-02 06:06:01.077Z | 2026-07-02 06:06:09.199Z |  |

**Incidents (SQLite `incidents` table):**

| feed | kind | count | first | last |
|---|---|---|---|---|
| clob_market | reconnect | 60 | 2026-07-02 06:01:38.909Z | 2026-07-02 10:59:00.317Z |
| rtds_chainlink | reconnect | 2 | 2026-07-02 08:40:22.548Z | 2026-07-02 10:40:23.152Z |

The 60 `clob_market` reconnects against 61 completed windows are **by design**: the CLOB market channel accepts only one in-place subscription update per connection (FACTS.md 4.7, verified live), so the feed performs a clean reconnect at every 5-minute window rollover to subscribe the next market's tokens.

`rtds_chainlink` reconnects:

- 2026-07-02 08:40:22.548Z — connection lost
- 2026-07-02 10:40:23.152Z — connection lost

The spacing (2h00m ± seconds) points to a server-enforced RTDS connection lifetime rather than network trouble; each reconnect's backfill dump recovered the prints from the gap (FACTS.md 5.8).

Recorder-side integrity: 11,420,135 raw lines scanned, 0 unparseable (torn tail of the in-progress hour file is expected), 9 empty RTDS connection-ack frames (FACTS.md 5.7), no `recorder_drop` incidents.

## 2. Price-to-beat (K) accuracy — the killer test

**K coverage:** 61/61 5-minute boundaries inside the recording span got a K captured. In addition the partial startup window (2026-07-02 05:55:00.000Z) — already running when the recorder came up — got a late K and is analyzed below but excluded from coverage.

**K capture lag:**

| population | n | median | p95 | max |
|---|---|---|---|---|
| oracle stamp − boundary, all windows | 62 | 0 ms | 68150 ms | 205000 ms |
| oracle stamp − boundary, post-fix | 52 | 0 ms | 0 ms | 1000 ms |
| received-by-bot − boundary, post-fix | 52 | 1419 ms | 1894 ms | 2667 ms |

Post-fix = windows from 2026-07-02 06:45:00.000Z on; earlier windows ran the RTDS parser bugs fixed in `3d645c5` (deployed 06:40:21Z) and are quarantined above rather than mixed into the health signal. The received-by-bot row is the operative number: how long after the boundary the strategy actually knows the price to beat.

**K cross-check:** 5 window(s) where the bot's captured K differs from the true boundary print reconstructed from the raw archive (live capture was late or wrong; reconnect backfill recovered the real print):

| window (UTC) | K table (lag ms) | K archive (print at boundary +ms) | Δ | pre-fix? |
|---|---|---|---|---|
| 2026-07-02 06:10:00.000Z | 60362.03 (70000) | 60354.92 (+0) | +7.10 | yes |
| 2026-07-02 06:15:00.000Z | 60366.17 (147000) | 60334.24 (+0) | +31.92 | yes |
| 2026-07-02 06:20:00.000Z | 60455.72 (205000) | 60356.46 (+0) | +99.26 | yes |
| 2026-07-02 06:30:00.000Z | 60421.54 (33000) | 60398.63 (+0) | +22.91 | yes |
| 2026-07-02 06:40:00.000Z | 60495.86 (22000) | 60498.24 (+0) | -2.38 | yes |

(For 2026-07-02 05:55:00.000Z the archive itself has no print near the boundary — first print 109s after it — recording started mid-window, so no ground-truth K exists for it.)

**Outcome agreement vs Gamma (killer test):** 60 resolved windows compared.

| view | agree | compared | rate |
|---|---|---|---|
| bot's live K (what the engine believed) | 58 | 60 | **96.7%** |
| post-fix windows only, bot's live K | 50 | 50 | **100.0%** |
| recorded archive (backfill-recovered K) | 59 | 59 | **100.0%** |

(1 window(s) excluded for having no boundary close print in the archive yet, a further 0 for not being resolved on Gamma yet.)

**Every disagreement (bot-view):**

| window (UTC) | K (lag ms) | close | margin $ | implied | archive-implied | Gamma | pre-fix? |
|---|---|---|---|---|---|---|---|
| 2026-07-02 06:15:00.000Z | 60366.17 (147000) | 60356.46 | -9.70 | DOWN | UP | UP | yes |
| 2026-07-02 06:20:00.000Z | 60455.72 (205000) | 60376.92 | -78.80 | DOWN | UP | UP | yes |

Excluded windows: 2026-07-02 10:55:00.000Z (no close print in archive yet)

## 3. Oracle cadence (Chainlink via RTDS)

17,620 deduplicated oracle prints (17,559 received live, the rest recovered via reconnect backfill), 2026-07-02 05:56:49.000Z → 2026-07-02 10:59:47.000Z.

| metric | median | p95 | p99 | max |
|---|---|---|---|---|
| oracle-series inter-print gap | 1000 ms | 1000 ms | 2000 ms | 9000 ms (2026-07-02 06:34:37.000Z → 2026-07-02 06:34:46.000Z) |
| as-received inter-frame gap (live) | 1055 ms | 1643 ms | 2339 ms | 8872 ms |

**Last-30s-of-window print density** over 60 boundaries: median **30**, mean 29.0, min 20 prints. This is the regime the late-window sniper operates in.

**Boundary-straddle gap** (last print before a boundary → first at/after): median 1000 ms, p95 1000 ms, max 3000 ms — the oracle-side floor on how quickly a window's K can exist at all.

No window had fewer than 10 prints in its final 30s — the oracle never went quiet where the strategy needs it most.

**RTDS delivery stalls (> 5 s without a frame): 13** (~2.6/h). In every stall the oracle kept printing and the buffered prints arrived late on the same connection (0 print(s) needed reconnect-backfill) — these are transport pauses, not data loss. 1 of them touched a window's final 30s (where the sniper would sit out on the `max_cl_age_ms` staleness guard):

| stall (s) | at | prints inside | tail touched |
|---|---|---|---|
| 8.45 | 2026-07-02 06:01:18.386Z | 2 | — |
| 8.77 | 2026-07-02 06:34:38.469Z | 2 | 2026-07-02 06:35:00.000Z |
| 7.73 | 2026-07-02 06:41:42.659Z | 3 | — |
| 7.38 | 2026-07-02 06:48:02.566Z | 2 | — |
| 8.62 | 2026-07-02 06:57:58.320Z | 2 | — |
| 8.57 | 2026-07-02 07:05:09.165Z | 2 | — |
| 7.26 | 2026-07-02 07:53:18.806Z | 3 | — |
| 8.74 | 2026-07-02 08:15:52.268Z | 3 | — |
| 8.87 | 2026-07-02 08:40:44.319Z | 3 | — |
| 7.40 | 2026-07-02 09:02:10.584Z | 2 | — |
| 8.07 | 2026-07-02 09:23:12.416Z | 2 | — |
| 7.48 | 2026-07-02 10:20:22.381Z | 2 | — |
| 8.35 | 2026-07-02 10:39:11.217Z | 2 | — |

## 4. Binance lead over the oracle

1-second log returns on a 100 ms LOCF grid over 181,178 grid points (local receive clocks, stale/reconnect stretches masked).

**Maximum cross-correlation: 0.681 at lag 2700 ms** (Binance leading; zero-lag correlation 0.022).

| lag (ms) | corr |
|---|---|
| 0 | 0.022 |
| 500 | 0.027 |
| 1000 | 0.056 |
| 1500 | 0.165 |
| 2000 | 0.407 |
| 2500 | 0.646 |
| 2700 | 0.681 ← max |
| 3000 | 0.627 |
| 3500 | 0.389 |
| 4000 | 0.192 |
| 4500 | 0.092 |
| 5000 | 0.035 |

**Basis (chainlink − binance mid)** at 17,559 live prints: mean **-80.17 USD**, std **7.50**, p5 -89.92, p95 -70.91. The level (~-13 bp) is expected: Binance quotes BTC/**USDT** while the oracle is BTC/**USD**, so the mean basis embeds the USDT/USD rate plus transport asymmetry. The model's `basis_window_s` EWMA carries the level; the std is what matters for edge sizing.

## 5. Verdict

# **GO**

**Evidence for:**

- ✅ outcome agreement: post-fix 50/50 = 100%; archive-view 59/59 = 100%; overall bot-view 96.7% (58/60).
- ✅ K captured at 61/61 boundaries (100%).
- ✅ post-fix K known to the bot median 1419 ms / p95 1894 ms / max 2667 ms after the boundary.
- ✅ oracle prints ~1/s (median gap 1000 ms); last-30s density median 30 prints, min 20.
- ✅ binance: 2,461,739 frames @ 136/s, 2 gap(s) over its 2s threshold.
- ✅ clob_market: 8,940,819 frames @ 493/s, 2 gap(s) over its 10s threshold.
- ✅ Binance leads the oracle: max corr 0.681 at 2700 ms (zero-lag 0.02).

**Concerns / conditions:**

- ⚠️ 2 disagreement(s) exist but only on pre-fix windows, and the recorded archive implies the correct outcome for them — fully explained by the parser bug fixed in 3d645c5 and closed by 100% post-fix + archive agreement.
- ⚠️ RTDS pauses >5s ~2.6×/h (max 8.9s); prints arrive late but none are lost. 1 touched a window tail — expect the sniper to sit out occasionally on the staleness guard. Track this rate in shadow.
- ⚠️ only 60 resolved windows (~5 h) vs the 24 h / ≥100-window target (FACTS.md 1.4) — shadow mode is itself the way to accumulate this; keep the recorder running and re-run this report before M2 calibration sign-off.

**Reproduce:** `python scripts/m1_extract.py --workdir WD && python scripts/m1_report.py --workdir WD` (read-only against the live recorder; Gamma responses cached in WD).
