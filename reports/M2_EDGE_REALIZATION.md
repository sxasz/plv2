# M2 Edge Realization Report — shadow GO/NO-GO gate

Generated 2026-07-04 19:41:33 UTC · shadow since 2026-07-02 16:15:00.000Z · config: p_min 0.93, price cap 0.95, min edge 0.02, stake $10, mode B on

**617 fully-observed windows** (592 settled on-engine, 25 not — 25 of these are the 2026-07-03 00:15–02:15 RTDS silent-connection incident, see the refreshed M1 report). All PnL below is from settled windows only; the one filled trade stranded by the incident is annotated separately in §2.

## a. Calibration (last armed-phase tick per window)

Brier **0.0403** vs always-predict-base-rate **0.2497** (base rate P(UP) = 0.517, n = 592 windows).

| P_up bucket | n | mean P_up | empirical UP rate |
|---|---|---|---|
| [0.0,0.1) | 228 | 0.008 | 0.013 |
| [0.1,0.2) | 19 | 0.144 | 0.105 |
| [0.2,0.3) | 12 | 0.253 | 0.000 |
| [0.3,0.4) | 14 | 0.356 | 0.143 |
| [0.4,0.5) | 11 | 0.455 | 0.545 |
| [0.5,0.6) | 21 | 0.548 | 0.714 |
| [0.6,0.7) | 21 | 0.649 | 0.762 |
| [0.7,0.8) | 14 | 0.744 | 0.929 |
| [0.8,0.9) | 15 | 0.858 | 0.800 |
| [0.9,1.0) | 237 | 0.990 | 1.000 |

## b. Trades by mode (fills/windows tables only)

| | Mode A sniper | Mode B scalper |
|---|---|---|
| intents created | 100 | 0 |
| intents gate-blocked | 69 | 0 |
| orders sent | 31 | 0 |
| filled | 27 | 0 |
| expired unfilled (FAK) | 4 | 0 |
| settled trades | 26 | 0 |
| wins | 16 | 0 |
| win rate | 61.5% | — |
| stake filled (USDC) | 245.78 | 0.00 |
| gross PnL (before fees) | -43.39 | +0.00 |
| fees realized (USDC) | 3.10 | 0.00 |
| **net PnL (USDC)** | **-46.49** | **+0.00** |

Fees are charged in shares on buys (FACTS.md 2.4); 'fees realized' values them at $1 on winning positions, $0 on losers. Total fee-shares charged: 7.15.

Daily net PnL (UTC days; the risk manager cuts trading at −$30 realized — `daily_loss_budget`):

| day | traded windows | net USDC |
|---|---|---|
| 2026-07-02 | 2 | +4.99 |
| 2026-07-03 | 9 | -23.70 |
| 2026-07-04 | 15 | -27.78 |

Mode B created **zero intents** in the whole run, and the numbers say why: its impulse trigger (43 USD in 1500 ms) is **17σ** of the observed short-horizon volatility (σ ≈ 2.6 USD per window). As configured it can never fire; it needs a retune or removal before it tells us anything.

Gate-blocked intents by gate: {'daily_loss_budget': 61, 'binance_stale': 4, 'below_min_size': 9, 'clock_unsynced': 2}

Ledger check: every settled trade's PnL reproduces exactly from its fills (cost, share-fee, redemption) — accounting is internally consistent.

⚠️ **Stranded trade** 2026-07-03 00:10:00.000Z: BUY UP 11.24 sh @ 0.89 — window never settled on-engine (missing close K). Gamma says the market resolved **UP** ⇒ hypothetical PnL +1.13 USDC (excluded from all totals above; kept as a data-integrity exhibit).

## c. Fee-drag decomposition (estimate — uses logged model_p)

Over the 26 settled fills with a logged intent:

| line | USDC |
|---|---|
| PnL had entry been at model-fair price (p_model) | -215.01 |
| PnL at actual fill price, before fees | -43.39 |
| PnL after fees (= ledger net) | -46.49 |

Price capture vs model (fill better/worse than fair): **+171.62**; fee drag: **-3.10**. The first line is the model's own claimed edge realized against actual outcomes — if it is already negative, the model, not execution, is the problem.

## d. Net PnL by entry bucket

**entry_price:**

| band | n | win rate | net USDC |
|---|---|---|---|
| <0.85 | 18 | 50.0% | -42.12 |
| 0.85-0.90 | 3 | 66.7% | -7.27 |
| 0.90-0.95 | 2 | 100.0% | +1.47 |
| >0.95 | 3 | 100.0% | +1.43 |

**tau_at_entry:**

| band | n | win rate | net USDC |
|---|---|---|---|
| 5-10s | 2 | 100.0% | +3.73 |
| 10-20s | 24 | 58.3% | -50.22 |

The gradient is the adverse-selection signature: the *cheaper* the ask was relative to the model's ≥0.93 claim, the worse the trade (50% wins below 0.85 vs 100% at 0.90–0.95). When the model and a live quote disagree, the quote has been right more often than the model.
## e. Missed signals (armed windows with model P ≥ 0.93 and no trade)

489 of 593 armed windows had at least one tick with P ≥ 0.93; the sniper traded 31 of them. Windows that qualified but never traded, by the blocker at their most-confident tick:

| blocker | windows | (tick-level count) |
|---|---|---|
| no_ask_quoted | 369 | 583,938 |
| ask_above_price_cap | 50 | 124,484 |
| gate:daily_loss_budget | 26 | 50,083 |
| size_below_min | 7 | 11,406 |
| gate:binance_stale | 3 | 6,263 |
| gate:clock_unsynced | 1 | 390 |
| edge_below_min_after_fees | 1 | 6,979 |
| gate:below_min_size | 1 | 3,293 |

Examples:

| window (UTC) | best P | side | ask | blocker | outcome |
|---|---|---|---|---|---|
| 2026-07-02 16:15:00.000Z | 0.999 | DOWN | — | no_ask_quoted | DOWN |
| 2026-07-02 16:25:00.000Z | 0.999 | DOWN | — | no_ask_quoted | DOWN |
| 2026-07-02 16:30:00.000Z | 0.999 | UP | — | no_ask_quoted | UP |
| 2026-07-02 16:35:00.000Z | 0.999 | UP | — | no_ask_quoted | UP |
| 2026-07-02 16:40:00.000Z | 0.999 | UP | — | no_ask_quoted | UP |
| 2026-07-02 16:45:00.000Z | 0.999 | UP | — | no_ask_quoted | UP |
| 2026-07-02 16:50:00.000Z | 0.999 | DOWN | — | no_ask_quoted | DOWN |
| 2026-07-02 17:05:00.000Z | 0.999 | UP | — | no_ask_quoted | UP |
| 2026-07-02 17:10:00.000Z | 0.999 | UP | — | no_ask_quoted | UP |
| 2026-07-02 17:15:00.000Z | 0.999 | UP | — | no_ask_quoted | UP |
| 2026-07-02 17:20:00.000Z | 0.992 | UP | — | no_ask_quoted | UP |
| 2026-07-02 17:30:00.000Z | 0.999 | DOWN | — | no_ask_quoted | DOWN |

## f. Counterfactual threshold sweep — **ESTIMATE ONLY**

Replay of armed ticks assuming a fill at the quoted ask up to visible size × depth-participation, same fee formula, freshness/tie gates applied, one shot per window, settled at the actual outcome. **No RTT, no adverse selection between tick and fill — treat these as upper bounds.**

| p_min \ price cap | ≤0.95 | ≤0.96 |
|---|---|---|
| **0.88** | n=112, 76% win, net -7.20 (avg ask 0.761, avg claimed p 0.956) | n=116, 77% win, net -6.27 (avg ask 0.768, avg claimed p 0.958) |
| **0.90** | n=110, 75% win, net -21.82 (avg ask 0.764, avg claimed p 0.959) | n=114, 76% win, net -20.90 (avg ask 0.771, avg claimed p 0.960) |
| **0.92** | n=103, 75% win, net -45.83 (avg ask 0.773, avg claimed p 0.965) | n=107, 76% win, net -44.90 (avg ask 0.780, avg claimed p 0.966) |
| **0.94** | n=93, 74% win, net -55.02 (avg ask 0.773, avg claimed p 0.973) | n=97, 75% win, net -54.10 (avg ask 0.781, avg claimed p 0.974) |

Read the win-rate column against 'avg claimed p': in 8/8 cells the realized win rate undershoots the model's claimed probability by more than 5 points. Conditioning on an ask being available below the cap selects exactly the moments the market disagrees with the model (adverse selection). No cell in this grid is net positive, even with optimistic fill assumptions.

## g. Statistics (10,000-resample bootstrap of per-trade net PnL)

- n = 26 settled trades, mean -1.788 ± 6.728 USDC/trade
- P(true mean > 0 rejected): p = 0.9124 for mean>0 claim; P(true mean ≥ 0) = 0.0876
- 95% CI of total net PnL: [-111.22, +22.82] USDC
- **26 trades is below the 30-trade floor — no significance claim either way**

## h. Latency (µs per hop, from latency_samples)

| hop | n | p50 | p95 | p99 |
|---|---|---|---|---|
| decision_to_sent | 31 | 1,098 | 5,237 | 10,983 |
| event_to_decision | 31 | 73 | 174 | 200 |
| sent_to_ack | 31 | 17 | 31 | 45 |

## i. Verdict

# **NO-GO**

- calibration at the final tick: Brier 0.0403 vs base-rate 0.2497 over 592 windows — informative
- realized: net -46.49 USDC over 26 settled trades (mean -1.79 ± 6.73/trade; bootstrap P(true mean ≥ 0) = 0.0876 — not significant at 5% on its own)
- mechanism identified, not noise: every threshold-sweep cell (n up to 116 windows) is net-negative under optimistic fill assumptions; realized win rates undershoot the model's claimed probabilities by >5 points in most cells; and the fee-drag decomposition shows the model's own claimed edge realizes to -215.01 USDC before any execution or fee effects — conditional on an ask existing, the model is overconfident (adverse selection), and no threshold in the sweep fixes it

**Recommendation:**

1. **Do not take Mode A to live trading.** The taker-sniper premise — that a cheap late-window ask is free money when the model is confident — is refuted by this data: available asks are informative, and everything the model wants to lift is priced by someone who has been right more often than us.
2. **Keep the shadow stack running unchanged** (it costs nothing) while pivoting design work to **Mode C (passive maker)**: the 367 no-ask windows are windows where the market *paid* whoever was quoting the winning side; makers pay zero fees and earn the spread the sniper has been paying. Its shadow fill model (queue-behind-visible-size) is already implemented.
3. **Fix the RTDS silent-connection failure** (treat `Too Many Requests` as fatal, add a no-data watchdog ~30 s, settle stranded sessions from Gamma) before any live milestone — it stranded 25 windows and a position in this run.
4. **Retune or disable Mode B** — its trigger is unreachable (see §b) so it is currently dead weight in every report.
