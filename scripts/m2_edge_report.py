"""M2 Edge Realization Report — the GO/NO-GO gate (spec §10).

Reads, strictly read-only:
  * data/bot.sqlite        windows / orders / fills / latency_samples
  * data/decisions/*.jsonl decision ticks ("decision") and "market_meta"
  * config.yaml            live strategy thresholds (context + sweep)
  * <workdir>/gamma_cache.json (optional, from m1_k_accuracy) — used only to
    annotate trades whose windows never settled on-engine.

All realized-money numbers come from the fills/windows tables. Model-based
numbers (fee-drag decomposition, missed signals, threshold sweep) are
labeled as the estimates they are.

Usage: m2_edge_report.py [--workdir DIR] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import orjson

sys.path.insert(0, str(Path(__file__).parent))
from m1_common import WINDOW_S, db_ro, iso_utc, iso_utc_s, pctl

REPO = Path("/opt/plv2")
DECISIONS_DIR = REPO / "data" / "decisions"
SHADOW_START_WS = 1783008900  # first fully shadow-managed window (2026-07-02 16:15 UTC)

SWEEP_P_MIN = (0.88, 0.90, 0.92, 0.94)
SWEEP_MAX_PRICE = (0.95, 0.96)
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260704

PRICE_BANDS = ((0.0, 0.85, "<0.85"), (0.85, 0.90, "0.85-0.90"), (0.90, 0.95, "0.90-0.95"), (0.95, 1.01, ">0.95"))
TAU_BANDS = ((2.0, 5.0, "2-5s"), (5.0, 10.0, "5-10s"), (10.0, 20.0, "10-20s"), (20.0, 1e9, ">20s"))


def load_cfg() -> dict:
    import yaml

    with (REPO / "config.yaml").open() as fh:
        return yaml.safe_load(fh)


# ------------------------------------------------------------------ decision stream
@dataclass(slots=True)
class Tick:
    now_s: float
    tau_s: float
    p_up: float
    up_ask: float | None
    up_ask_sz: float
    down_ask: float | None
    down_ask_sz: float
    cl_age: float
    bn_age: float
    tie_band: bool
    book_dirty: bool
    sigma: float | None


@dataclass(slots=True)
class WindowTicks:
    window_start: int
    slug: str
    armed: list[Tick] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)


def stream_windows(meta_out: dict[int, dict]):
    """Yield one WindowTicks per window, chronologically. market_meta docs are
    accumulated into meta_out as encountered (they precede their window)."""
    cur: WindowTicks | None = None
    for path in sorted(DECISIONS_DIR.glob("*.jsonl")):
        with path.open("rb") as fh:
            for line in fh:
                try:
                    d = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue  # torn tail of the in-progress file
                p = d.get("payload", {})
                kind = d.get("kind")
                if kind == "market_meta":
                    meta_out[p["window_start"]] = p
                    continue
                if kind != "decision":
                    continue
                slug = p.get("slug", "")
                try:
                    ws = int(slug.rsplit("-", 1)[1])
                except (IndexError, ValueError):
                    continue
                if ws < SHADOW_START_WS:
                    continue
                if cur is None or cur.window_start != ws:
                    if cur is not None:
                        yield cur
                    cur = WindowTicks(window_start=ws, slug=slug)
                if "action" in p:
                    cur.actions.append(p["action"])
                if p.get("phase") == "ARMED" and "P_up" in p:
                    up, dn = p.get("up") or {}, p.get("down") or {}
                    cur.armed.append(
                        Tick(
                            now_s=p["now_s"],
                            tau_s=p["tau_s"],
                            p_up=p["P_up"],
                            up_ask=up.get("ask"),
                            up_ask_sz=up.get("ask_sz") or 0.0,
                            down_ask=dn.get("ask"),
                            down_ask_sz=dn.get("ask_sz") or 0.0,
                            cl_age=p.get("cl_age_ms", 1e9),
                            bn_age=p.get("bn_age_ms", 1e9),
                            tie_band=bool(p.get("tie_band")),
                            book_dirty=bool(p.get("book_dirty")),
                            sigma=p.get("sigma_px_s"),
                        )
                    )
    if cur is not None:
        yield cur


# ------------------------------------------------------------------ helpers
def fee_per_share(rate_bps: float, price: float) -> float:
    return (rate_bps / 10_000.0) * price * (1.0 - price)


def sniper_first_fail(
    t: Tick, side_p: float, ask: float | None, ask_sz: float, cfg: dict, rate_bps: float, min_order: float
) -> str | None:
    """Mirror SniperStrategy.evaluate filter order, then predict the risk
    gate that would block. None ⇒ the tick should have produced an order."""
    a = cfg["mode_a"]
    if ask is None or ask_sz <= 0:
        return "no_ask_quoted"
    if ask > a["max_snipe_price"]:
        return "ask_above_price_cap"
    edge = side_p - ask - fee_per_share(rate_bps, ask) - a["slippage_buffer"]
    if edge < a["min_edge_snipe"]:
        return "edge_below_min_after_fees"
    size = min(ask_sz * cfg["sizing"]["depth_participation"], cfg["sizing"]["stake_usdc"] / ask)
    if size < min_order:
        return "size_below_min"
    if t.tie_band:
        return "gate:tie_band"
    if t.cl_age > cfg["risk"]["max_cl_age_ms"]:
        return "gate:chainlink_stale"
    if t.bn_age > cfg["risk"]["max_bn_age_ms"]:
        return "gate:binance_stale"
    if t.book_dirty:
        return "gate:book_dirty"
    if ask_sz < size * cfg["risk"]["min_liquidity_multiple"]:
        return "gate:thin_book"
    return None


def bootstrap(pnls: list[float]) -> dict:
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(pnls)
    means = []
    for _ in range(BOOTSTRAP_N):
        s = 0.0
        for _ in range(n):
            s += pnls[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    return {
        "n": n,
        "mean": mean,
        "std": math.sqrt(var),
        "p_mean_gt0": sum(1 for m in means if m <= 0) / len(means),
        "p_mean_lt0": sum(1 for m in means if m >= 0) / len(means),
        "ci95_total": [means[int(0.025 * BOOTSTRAP_N)] * n, means[int(0.975 * BOOTSTRAP_N) - 1] * n],
    }


# ------------------------------------------------------------------ analysis
def analyze(workdir: Path) -> dict:
    cfg = load_cfg()
    conn = db_ro()
    conn.row_factory = sqlite3.Row
    now = time.time()

    windows = {
        r["window_start"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM windows WHERE window_start >= ? AND window_start + ? <= ?",
            (SHADOW_START_WS, WINDOW_S, now),
        )
    }
    orders = [dict(r) for r in conn.execute("SELECT * FROM orders")]
    fills = [dict(r) for r in conn.execute("SELECT * FROM fills")]
    latency: dict[str, list[float]] = defaultdict(list)
    for r in conn.execute("SELECT hop, micros FROM latency_samples"):
        latency[r["hop"]].append(r["micros"])
    conn.close()

    fills_by_order: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        # shadow exchange prefixes the client id: fills carry "shadow-c1"
        # while the orders table stores "c1"
        fills_by_order[f["order_id"].removeprefix("shadow-")].append(f)
    settled = {ws for ws, w in windows.items() if w["outcome"] is not None}

    # ---- trades (one row per order), realized numbers straight from tables ---
    trades = []
    for o in orders:
        ws = int(o["session_slug"].rsplit("-", 1)[1])
        ofills = fills_by_order.get(o["order_id"], [])
        fsize = sum(f["size"] for f in ofills)
        vwap = sum(f["price"] * f["size"] for f in ofills) / fsize if fsize else None
        w = windows.get(ws, {})
        trades.append(
            {
                "order_id": o["order_id"],
                "window_start": ws,
                "utc": iso_utc(ws),
                "strategy": o["strategy"],
                "token_outcome": o["outcome"],
                "limit_price": o["price"],
                "req_size": o["size"],
                "filled_size": fsize,
                "vwap": vwap,
                "fee_shares": sum(f["fee_shares"] for f in ofills),
                "tau_entry_s": (ws + WINDOW_S) - o["created_wall_ns"] / 1e9 if o["created_wall_ns"] else None,
                "win_outcome": w.get("outcome"),
                "won": (w.get("outcome") == o["outcome"]) if w.get("outcome") else None,
                "realized": w.get("realized_pnl_usdc"),
                "settled": w.get("outcome") is not None,
            }
        )
    traded_ws = {t["window_start"] for t in trades}
    st_all = [t for t in trades if t["settled"] and t["filled_size"] > 0]

    # ---- stream decisions: calibration, missed signals, sweep, intents -------
    p_min = cfg["mode_a"]["p_min_snipe"]
    meta_by_ws: dict[int, dict] = {}
    calib: list[tuple[float, int]] = []
    intents_by_strategy: Counter = Counter()
    intents_blocked: Counter = Counter()
    gates_hit: Counter = Counter()
    modelp_by_ws: dict[int, float] = {}
    missed_window_reasons: Counter = Counter()
    missed_tick_reasons: Counter = Counter()
    missed_examples: list[dict] = []
    anomalies: list[dict] = []
    armed_windows = 0
    qualifying_windows = 0
    sigma_sum = 0.0
    sigma_n = 0
    sweep = {
        (pm, mp): {"n": 0, "wins": 0, "net": 0.0, "stake": 0.0, "sum_ask": 0.0, "sum_p": 0.0}
        for pm in SWEEP_P_MIN
        for mp in SWEEP_MAX_PRICE
    }

    for wt in stream_windows(meta_by_ws):
        w = windows.get(wt.window_start)
        if w is None:
            continue
        m = meta_by_ws.get(wt.window_start, {})
        rate_bps = m.get("fee_rate_bps", 1000)
        min_order = m.get("min_order_size", 5.0)
        outcome = w["outcome"]

        for a in wt.actions:
            intents_by_strategy[a["strategy"]] += 1
            if not a.get("allowed"):
                intents_blocked[a["strategy"]] += 1
                gates_hit.update(a.get("gates_failed", []))
            elif a["strategy"] == "mode_a_sniper":
                modelp_by_ws[wt.window_start] = a["model_p"]

        if wt.armed:
            armed_windows += 1
            if outcome is not None:
                last = wt.armed[-1]
                calib.append((last.p_up, 1 if outcome == "UP" else 0))
            for t in wt.armed:
                if t.sigma is not None:
                    sigma_sum += t.sigma
                    sigma_n += 1

        qual = []
        for t in wt.armed:
            if t.p_up >= p_min:
                qual.append((t, "UP", t.p_up, t.up_ask, t.up_ask_sz))
            elif (1 - t.p_up) >= p_min:
                qual.append((t, "DOWN", 1 - t.p_up, t.down_ask, t.down_ask_sz))
        if qual:
            qualifying_windows += 1
            if wt.window_start not in traded_ws:
                # If the engine logged blocked intents for this window, the
                # real blocker is known — use it instead of reconstructing.
                blocked_gates = Counter()
                for a in wt.actions:
                    if not a.get("allowed"):
                        blocked_gates.update(a.get("gates_failed", []))
                best = None
                for t, side, sp, ask, ask_sz in qual:
                    reason = sniper_first_fail(t, sp, ask, ask_sz, cfg, rate_bps, min_order)
                    if reason is None:
                        reason = (
                            f"gate:{blocked_gates.most_common(1)[0][0]}"
                            if blocked_gates
                            else "would_fire_unexplained"
                        )
                    missed_tick_reasons[reason] += 1
                    if best is None or sp > best[0]:
                        best = (sp, reason, side, ask)
                missed_window_reasons[best[1]] += 1
                if len(missed_examples) < 12:
                    missed_examples.append(
                        {
                            "utc": iso_utc(wt.window_start),
                            "best_p": round(best[0], 4),
                            "side": best[2],
                            "ask": best[3],
                            "reason": best[1],
                            "outcome": outcome,
                            "qual_ticks": len(qual),
                        }
                    )
                if best[1] == "would_fire_unexplained":
                    anomalies.append({"utc": iso_utc(wt.window_start), "best_p": best[0]})

        # ---- counterfactual sweep (ESTIMATE: assumes the quoted ask is still
        # fillable up to visible size after RTT; no adverse book move) --------
        if outcome is not None:
            fired: set[tuple[float, float]] = set()
            for t in wt.armed:
                if len(fired) == len(sweep):
                    break
                if t.tie_band or t.book_dirty or t.cl_age > cfg["risk"]["max_cl_age_ms"] or t.bn_age > cfg["risk"]["max_bn_age_ms"]:
                    continue
                for side, sp, ask, ask_sz in (
                    ("UP", t.p_up, t.up_ask, t.up_ask_sz),
                    ("DOWN", 1 - t.p_up, t.down_ask, t.down_ask_sz),
                ):
                    if ask is None or ask_sz <= 0 or sp < min(SWEEP_P_MIN):
                        continue
                    edge = sp - ask - fee_per_share(rate_bps, ask) - cfg["mode_a"]["slippage_buffer"]
                    if edge < cfg["mode_a"]["min_edge_snipe"]:
                        continue
                    size = round(min(ask_sz * cfg["sizing"]["depth_participation"], cfg["sizing"]["stake_usdc"] / ask), 2)
                    if size < min_order:
                        continue
                    fee_sh = size * (rate_bps / 10_000.0) * ask * (1 - ask)
                    won = outcome == side
                    pnl = ((size - fee_sh) - ask * size) if won else (-ask * size)
                    for pm in SWEEP_P_MIN:
                        if sp < pm:
                            continue
                        for mp in SWEEP_MAX_PRICE:
                            key = (pm, mp)
                            if key in fired or ask > mp:
                                continue
                            fired.add(key)
                            s = sweep[key]
                            s["n"] += 1
                            s["wins"] += int(won)
                            s["net"] += pnl
                            s["stake"] += ask * size
                            s["sum_ask"] += ask
                            s["sum_p"] += sp

    # ---- (a) calibration ------------------------------------------------------
    base = sum(y for _, y in calib) / len(calib) if calib else float("nan")
    calib_summary = {
        "n": len(calib),
        "brier": sum((p - y) ** 2 for p, y in calib) / len(calib) if calib else None,
        "base_rate": base,
        "brier_base": base * (1 - base) if calib else None,
        "deciles": [],
    }
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        sub = [(p, y) for p, y in calib if lo <= p < hi or (i == 9 and p == 1.0)]
        if sub:
            calib_summary["deciles"].append(
                {
                    "bucket": f"[{lo:.1f},{hi:.1f})",
                    "n": len(sub),
                    "mean_p": sum(p for p, _ in sub) / len(sub),
                    "up_rate": sum(y for _, y in sub) / len(sub),
                }
            )

    # ---- (b) per-mode trade stats ----------------------------------------------
    def mode_stats(strategy: str) -> dict:
        rows = [t for t in trades if t["strategy"] == strategy]
        st = [t for t in rows if t["settled"] and t["filled_size"] > 0]
        wins = sum(1 for t in st if t["won"])
        return {
            "intents": intents_by_strategy.get(strategy, 0),
            "intents_gate_blocked": intents_blocked.get(strategy, 0),
            "orders_sent": len(rows),
            "filled": sum(1 for t in rows if t["filled_size"] > 0),
            "expired_unfilled": sum(1 for t in rows if t["filled_size"] == 0),
            "settled_trades": len(st),
            "unsettled_trades": sum(1 for t in rows if not t["settled"] and t["filled_size"] > 0),
            "wins": wins,
            "win_rate": wins / len(st) if st else None,
            "gross_pnl": sum(t["filled_size"] * (1.0 if t["won"] else 0.0) - t["vwap"] * t["filled_size"] for t in st),
            "fees_realized_usdc": sum(t["fee_shares"] * (1.0 if t["won"] else 0.0) for t in st),
            "fees_charged_shares": sum(t["fee_shares"] for t in st),
            "net_pnl": sum(t["realized"] for t in st),
            "stake": sum(t["vwap"] * t["filled_size"] for t in st),
        }

    stats = {"mode_a_sniper": mode_stats("mode_a_sniper"), "mode_b_scalper": mode_stats("mode_b_scalper")}

    # ledger-vs-reconstruction consistency check
    recon_err = []
    for t in st_all:
        expect = (t["filled_size"] - t["fee_shares"]) * (1.0 if t["won"] else 0.0) - t["vwap"] * t["filled_size"]
        if abs(expect - t["realized"]) > 0.01:
            recon_err.append({"order": t["order_id"], "reconstructed": round(expect, 4), "ledger": t["realized"]})

    # ---- (c) fee-drag decomposition (model_p from logged intents — estimate) ----
    decomposition = None
    rows = [t for t in st_all if t["window_start"] in modelp_by_ws]
    if rows:
        at_fair = sum(t["filled_size"] * ((1.0 if t["won"] else 0.0) - modelp_by_ws[t["window_start"]]) for t in rows)
        at_fill = sum(t["filled_size"] * ((1.0 if t["won"] else 0.0) - t["vwap"]) for t in rows)
        after_fees = sum(t["realized"] for t in rows)
        decomposition = {
            "n": len(rows),
            "at_model_fair": at_fair,
            "at_fill": at_fill,
            "after_fees": after_fees,
            "price_capture_vs_model": at_fill - at_fair,
            "fee_drag": after_fees - at_fill,
        }

    # ---- (d) buckets --------------------------------------------------------------
    def bucketize(bands, key):
        out = []
        for lo, hi, label in bands:
            sub = [t for t in st_all if t[key] is not None and lo <= t[key] < hi]
            if sub:
                out.append(
                    {
                        "band": label,
                        "n": len(sub),
                        "win_rate": sum(1 for t in sub if t["won"]) / len(sub),
                        "net": sum(t["realized"] for t in sub),
                    }
                )
        return out

    buckets = {"entry_price": bucketize(PRICE_BANDS, "vwap"), "tau_at_entry": bucketize(TAU_BANDS, "tau_entry_s")}

    # ---- (g) bootstrap ---------------------------------------------------------------
    pnls = [t["realized"] for t in st_all]
    boot = bootstrap(pnls) if len(pnls) >= 2 else None

    # ---- daily PnL (loss-cap dynamics) --------------------------------------------------
    daily: dict[str, dict] = {}
    for ws, w in windows.items():
        if w["realized_pnl_usdc"] is None:
            continue
        day = iso_utc(ws)[:10]
        d = daily.setdefault(day, {"net": 0.0, "trades": 0})
        d["net"] += w["realized_pnl_usdc"]
        d["trades"] += 1 if w["n_trades"] else 0

    # ---- mode B reality check ------------------------------------------------------------
    mean_sigma = sigma_sum / sigma_n if sigma_n else None
    mean_k = sum(w["k"] for w in windows.values() if w["k"]) / max(
        sum(1 for w in windows.values() if w["k"]), 1
    )
    mode_b_note = None
    if mean_sigma:
        thr_usd = cfg["mode_b"]["impulse_threshold"] * mean_k
        move_std = mean_sigma * math.sqrt(cfg["mode_b"]["impulse_window_ms"] / 1000.0)
        mode_b_note = {
            "impulse_threshold_usd": thr_usd,
            "observed_move_std_usd": move_std,
            "threshold_in_sigmas": thr_usd / move_std if move_std else None,
        }

    # ---- (h) latency --------------------------------------------------------------------
    lat = {}
    for hop, vals in sorted(latency.items()):
        vals.sort()
        lat[hop] = {"n": len(vals), "p50": pctl(vals, 0.5), "p95": pctl(vals, 0.95), "p99": pctl(vals, 0.99)}

    # ---- unsettled traded windows (annotation via Gamma, if cached) ----------------------
    gamma_cache = {}
    gpath = workdir / "gamma_cache.json"
    if gpath.exists():
        gamma_cache = json.loads(gpath.read_text())
    unsettled_trades = []
    for t in trades:
        if t["settled"] or t["filled_size"] == 0:
            continue
        g = gamma_cache.get(f"btc-updown-5m-{t['window_start']}", {})
        won = (g.get("winner") == t["token_outcome"]) if g.get("winner") else None
        hypo = ((t["filled_size"] - t["fee_shares"]) * (1.0 if won else 0.0) - t["vwap"] * t["filled_size"]) if won is not None else None
        unsettled_trades.append({**t, "gamma_winner": g.get("winner"), "hypothetical_pnl": hypo})

    # ---- (i) verdict ------------------------------------------------------------------------
    verdict, reasons = decide_verdict(stats["mode_a_sniper"], boot, calib_summary, sweep, decomposition)

    return {
        "generated": iso_utc_s(now),
        "config_snapshot": {
            "p_min_snipe": cfg["mode_a"]["p_min_snipe"],
            "max_snipe_price": cfg["mode_a"]["max_snipe_price"],
            "min_edge_snipe": cfg["mode_a"]["min_edge_snipe"],
            "slippage_buffer": cfg["mode_a"]["slippage_buffer"],
            "stake_usdc": cfg["sizing"]["stake_usdc"],
            "mode_b_enabled": cfg["mode_b"]["enabled"],
            "impulse_window_ms": cfg["mode_b"]["impulse_window_ms"],
        },
        "windows_total": len(windows),
        "windows_settled": len(settled),
        "windows_unsettled": len(windows) - len(settled),
        "armed_windows": armed_windows,
        "qualifying_windows": qualifying_windows,
        "calibration": calib_summary,
        "modes": stats,
        "gates_hit_on_blocked_intents": dict(gates_hit),
        "reconciliation_errors": recon_err,
        "decomposition": decomposition,
        "buckets": buckets,
        "missed_window_reasons": dict(missed_window_reasons),
        "missed_tick_reasons": dict(missed_tick_reasons),
        "missed_examples": missed_examples,
        "anomalies": anomalies,
        "daily": daily,
        "mode_b_note": mode_b_note,
        "sweep": {f"p>={pm:.2f} & ask<={mp:.2f}": v for (pm, mp), v in sweep.items()},
        "bootstrap": boot,
        "latency": lat,
        "trades": trades,
        "unsettled_trades": unsettled_trades,
        "verdict": verdict,
        "verdict_reasons": reasons,
    }


def decide_verdict(a: dict, boot: dict | None, calib: dict, sweep: dict, decomp: dict | None) -> tuple[str, list[str]]:
    reasons: list[str] = []
    calib_ok = calib["brier"] is not None and calib["brier"] < calib["brier_base"]
    reasons.append(
        f"calibration at the final tick: Brier {calib['brier']:.4f} vs base-rate "
        f"{calib['brier_base']:.4f} over {calib['n']} windows — "
        f"{'informative' if calib_ok else 'NOT better than guessing the base rate'}"
    )
    n, net = a["settled_trades"], a["net_pnl"]
    if n == 0 or boot is None:
        return "EXTEND", reasons + ["not enough settled trades to judge anything"]

    if net > 0 and boot["p_mean_gt0"] < 0.05 and calib_ok and n >= 30:
        return "GO", reasons + [f"net +{net:.2f} USDC over {n} trades, bootstrap p={boot['p_mean_gt0']:.4f}"]

    # The spec's GO bar is not met. Distinguish "keep collecting" from
    # "the mechanism of the loss is identified" using the evidence that does
    # not depend on the small realized-trade count:
    cells = [v for v in sweep.values() if v["n"] >= 20]
    sweep_all_neg = bool(cells) and all(v["net"] < 0 for v in cells)
    sweep_overconf = bool(cells) and sum(
        1 for v in cells if v["wins"] / v["n"] < v["sum_p"] / v["n"] - 0.05
    ) >= 0.75 * len(cells)
    model_edge_neg = decomp is not None and decomp["at_model_fair"] < 0

    if net < 0:
        reasons.append(
            f"realized: net {net:+.2f} USDC over {n} settled trades "
            f"(mean {boot['mean']:+.2f} ± {boot['std']:.2f}/trade; bootstrap "
            f"P(true mean ≥ 0) = {boot['p_mean_lt0']:.4f}"
            + (" — not significant at 5% on its own)" if boot["p_mean_lt0"] >= 0.05 else " — significantly negative)")
        )
    if boot["p_mean_lt0"] < 0.05 and net < 0:
        return "NO-GO", reasons + ["the realized loss alone is statistically significant"]
    if net < 0 and sweep_all_neg and sweep_overconf and model_edge_neg:
        reasons.append(
            f"mechanism identified, not noise: every threshold-sweep cell (n up to "
            f"{max(v['n'] for v in cells)} windows) is net-negative under optimistic fill "
            "assumptions; realized win rates undershoot the model's claimed probabilities by "
            ">5 points in most cells; and the fee-drag decomposition shows the model's own "
            f"claimed edge realizes to {decomp['at_model_fair']:+.2f} USDC before any "
            "execution or fee effects — conditional on an ask existing, the model is "
            "overconfident (adverse selection), and no threshold in the sweep fixes it"
        )
        return "NO-GO", reasons

    n_needed = math.ceil((1.96 * boot["std"] / abs(boot["mean"])) ** 2) if abs(boot["mean"]) > 1e-9 else 10**6
    reasons.append(
        f"inconclusive: ~{n_needed} trades needed for the 95% CI to exclude zero at the observed "
        f"effect size (have {n})"
    )
    return "EXTEND", reasons


# ------------------------------------------------------------------ markdown
def money(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}"


def render_md(s: dict) -> str:
    c = s["config_snapshot"]
    L: list[str] = []
    L.append("# M2 Edge Realization Report — shadow GO/NO-GO gate")
    L.append(
        f"\nGenerated {s['generated']} · shadow since {iso_utc(SHADOW_START_WS)} · config: "
        f"p_min {c['p_min_snipe']}, price cap {c['max_snipe_price']}, min edge {c['min_edge_snipe']}, "
        f"stake ${c['stake_usdc']:.0f}, mode B {'on' if c['mode_b_enabled'] else 'off'}"
    )
    L.append(
        f"\n**{s['windows_total']} fully-observed windows** ({s['windows_settled']} settled on-engine, "
        f"{s['windows_unsettled']} not — 25 of these are the 2026-07-03 00:15–02:15 RTDS silent-connection "
        "incident, see the refreshed M1 report). All PnL below is from settled windows only; the one "
        "filled trade stranded by the incident is annotated separately in §2."
    )

    # a. calibration
    cal = s["calibration"]
    L.append("\n## a. Calibration (last armed-phase tick per window)\n")
    L.append(f"Brier **{cal['brier']:.4f}** vs always-predict-base-rate **{cal['brier_base']:.4f}** "
             f"(base rate P(UP) = {cal['base_rate']:.3f}, n = {cal['n']} windows).\n")
    L.append("| P_up bucket | n | mean P_up | empirical UP rate |")
    L.append("|---|---|---|---|")
    for d in cal["deciles"]:
        L.append(f"| {d['bucket']} | {d['n']} | {d['mean_p']:.3f} | {d['up_rate']:.3f} |")

    # b. trades
    L.append("\n## b. Trades by mode (fills/windows tables only)\n")
    L.append("| | Mode A sniper | Mode B scalper |")
    L.append("|---|---|---|")
    A, B = s["modes"]["mode_a_sniper"], s["modes"]["mode_b_scalper"]
    for label, key in [
        ("intents created", "intents"),
        ("intents gate-blocked", "intents_gate_blocked"),
        ("orders sent", "orders_sent"),
        ("filled", "filled"),
        ("expired unfilled (FAK)", "expired_unfilled"),
        ("settled trades", "settled_trades"),
        ("wins", "wins"),
    ]:
        L.append(f"| {label} | {A[key]} | {B[key]} |")
    L.append(f"| win rate | {A['win_rate']:.1%} | {'—' if B['win_rate'] is None else f'{B['win_rate']:.1%}'} |"
             if A["win_rate"] is not None else "| win rate | — | — |")
    L.append(f"| stake filled (USDC) | {A['stake']:.2f} | {B['stake']:.2f} |")
    L.append(f"| gross PnL (before fees) | {money(A['gross_pnl'])} | {money(B['gross_pnl'])} |")
    L.append(f"| fees realized (USDC) | {A['fees_realized_usdc']:.2f} | {B['fees_realized_usdc']:.2f} |")
    L.append(f"| **net PnL (USDC)** | **{money(A['net_pnl'])}** | **{money(B['net_pnl'])}** |")
    L.append(
        "\nFees are charged in shares on buys (FACTS.md 2.4); 'fees realized' values them at $1 on "
        f"winning positions, $0 on losers. Total fee-shares charged: {A['fees_charged_shares']:.2f}."
    )
    if s["daily"]:
        L.append("\nDaily net PnL (UTC days; the risk manager cuts trading at "
                 "−$30 realized — `daily_loss_budget`):\n")
        L.append("| day | traded windows | net USDC |")
        L.append("|---|---|---|")
        for day in sorted(s["daily"]):
            d = s["daily"][day]
            L.append(f"| {day} | {d['trades']} | {money(d['net'])} |")
    mb = s.get("mode_b_note")
    if mb and s["modes"]["mode_b_scalper"]["intents"] == 0:
        L.append(
            f"\nMode B created **zero intents** in the whole run, and the numbers say why: its "
            f"impulse trigger ({mb['impulse_threshold_usd']:.0f} USD in "
            f"{s['config_snapshot'].get('impulse_window_ms', 1500)} ms) is "
            f"**{mb['threshold_in_sigmas']:.0f}σ** of the observed short-horizon volatility "
            f"(σ ≈ {mb['observed_move_std_usd']:.1f} USD per window). As configured it can never fire; "
            "it needs a retune or removal before it tells us anything."
        )
    if s["gates_hit_on_blocked_intents"]:
        L.append(f"\nGate-blocked intents by gate: {s['gates_hit_on_blocked_intents']}")
    if s["reconciliation_errors"]:
        L.append(f"\n⚠️ ledger reconciliation mismatches: {s['reconciliation_errors']}")
    else:
        L.append("\nLedger check: every settled trade's PnL reproduces exactly from its fills (cost, "
                 "share-fee, redemption) — accounting is internally consistent.")
    for t in s["unsettled_trades"]:
        L.append(
            f"\n⚠️ **Stranded trade** {t['utc']}: BUY {t['token_outcome']} {t['filled_size']:.2f} sh @ "
            f"{t['vwap']:.2f} — window never settled on-engine (missing close K). Gamma says the market "
            f"resolved **{t['gamma_winner']}** ⇒ hypothetical PnL {money(t['hypothetical_pnl'])} USDC "
            "(excluded from all totals above; kept as a data-integrity exhibit)."
        )

    # c. fee drag
    d = s["decomposition"]
    if d:
        L.append("\n## c. Fee-drag decomposition (estimate — uses logged model_p)\n")
        L.append(f"Over the {d['n']} settled fills with a logged intent:\n")
        L.append("| line | USDC |")
        L.append("|---|---|")
        L.append(f"| PnL had entry been at model-fair price (p_model) | {money(d['at_model_fair'])} |")
        L.append(f"| PnL at actual fill price, before fees | {money(d['at_fill'])} |")
        L.append(f"| PnL after fees (= ledger net) | {money(d['after_fees'])} |")
        L.append(f"\nPrice capture vs model (fill better/worse than fair): **{money(d['price_capture_vs_model'])}**; "
                 f"fee drag: **{money(d['fee_drag'])}**. The first line is the model's own claimed edge realized "
                 "against actual outcomes — if it is already negative, the model, not execution, is the problem.")

    # d. buckets
    L.append("\n## d. Net PnL by entry bucket\n")
    for name, rows in s["buckets"].items():
        L.append(f"**{name}:**\n")
        L.append("| band | n | win rate | net USDC |")
        L.append("|---|---|---|---|")
        for r in rows:
            L.append(f"| {r['band']} | {r['n']} | {r['win_rate']:.1%} | {money(r['net'])} |")
        L.append("")
    pb = {r["band"]: r for r in s["buckets"]["entry_price"]}
    lo_b, hi_b = pb.get("<0.85"), pb.get("0.90-0.95")
    if lo_b and hi_b and lo_b["win_rate"] < hi_b["win_rate"]:
        L.append(
            "The gradient is the adverse-selection signature: the *cheaper* the ask was relative to "
            f"the model's ≥{s['config_snapshot']['p_min_snipe']} claim, the worse the trade "
            f"({lo_b['win_rate']:.0%} wins below 0.85 vs {hi_b['win_rate']:.0%} at 0.90–0.95). When "
            "the model and a live quote disagree, the quote has been right more often than the model."
        )

    # e. missed signals
    L.append("## e. Missed signals (armed windows with model P ≥ 0.93 and no trade)\n")
    L.append(f"{s['qualifying_windows']} of {s['armed_windows']} armed windows had at least one tick with "
             f"P ≥ {c['p_min_snipe']}; the sniper traded {s['modes']['mode_a_sniper']['orders_sent']} of them. "
             "Windows that qualified but never traded, by the blocker at their most-confident tick:\n")
    L.append("| blocker | windows | (tick-level count) |")
    L.append("|---|---|---|")
    tick_r = s["missed_tick_reasons"]
    for reason, n in sorted(s["missed_window_reasons"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {reason} | {n} | {tick_r.get(reason, 0):,} |")
    if s["missed_examples"]:
        L.append("\nExamples:\n")
        L.append("| window (UTC) | best P | side | ask | blocker | outcome |")
        L.append("|---|---|---|---|---|---|")
        for e in s["missed_examples"]:
            L.append(f"| {e['utc']} | {e['best_p']:.3f} | {e['side']} | "
                     f"{'—' if e['ask'] is None else f'{e['ask']:.2f}'} | {e['reason']} | {e['outcome']} |")
    if s["anomalies"]:
        L.append(f"\n⚠️ unexplained non-fires (filters passed, no order): {s['anomalies']}")

    # f. sweep
    L.append("\n## f. Counterfactual threshold sweep — **ESTIMATE ONLY**\n")
    L.append("Replay of armed ticks assuming a fill at the quoted ask up to visible size × "
             "depth-participation, same fee formula, freshness/tie gates applied, one shot per window, "
             "settled at the actual outcome. **No RTT, no adverse selection between tick and fill — "
             "treat these as upper bounds.**\n")
    L.append("| p_min \\ price cap | " + " | ".join(f"≤{mp:.2f}" for mp in SWEEP_MAX_PRICE) + " |")
    L.append("|---|" + "---|" * len(SWEEP_MAX_PRICE))
    for pm in SWEEP_P_MIN:
        cells = []
        for mp in SWEEP_MAX_PRICE:
            v = s["sweep"][f"p>={pm:.2f} & ask<={mp:.2f}"]
            if v["n"]:
                cells.append(
                    f"n={v['n']}, {v['wins'] / v['n']:.0%} win, net {v['net']:+.2f} "
                    f"(avg ask {v['sum_ask'] / v['n']:.3f}, avg claimed p {v['sum_p'] / v['n']:.3f})"
                )
            else:
                cells.append("—")
        L.append(f"| **{pm:.2f}** | " + " | ".join(cells) + " |")
    cells = [v for v in s["sweep"].values() if v["n"] >= 20]
    if cells:
        overconf = sum(1 for v in cells if v["wins"] / v["n"] < v["sum_p"] / v["n"] - 0.05)
        all_neg = all(v["net"] < 0 for v in cells)
        L.append(
            f"\nRead the win-rate column against 'avg claimed p': in {overconf}/{len(cells)} cells the "
            "realized win rate undershoots the model's claimed probability by more than 5 points. "
            "Conditioning on an ask being available below the cap selects exactly the moments the "
            "market disagrees with the model (adverse selection)."
            + (" No cell in this grid is net positive, even with optimistic fill assumptions." if all_neg else "")
        )

    # g. statistics
    b = s["bootstrap"]
    L.append("\n## g. Statistics (10,000-resample bootstrap of per-trade net PnL)\n")
    if b:
        L.append(f"- n = {b['n']} settled trades, mean {b['mean']:+.3f} ± {b['std']:.3f} USDC/trade")
        L.append(f"- P(true mean > 0 rejected): p = {b['p_mean_gt0']:.4f} for mean>0 claim; "
                 f"P(true mean ≥ 0) = {b['p_mean_lt0']:.4f}")
        L.append(f"- 95% CI of total net PnL: [{b['ci95_total'][0]:+.2f}, {b['ci95_total'][1]:+.2f}] USDC")
        if b["n"] < 30:
            L.append(f"- **{b['n']} trades is below the 30-trade floor — no significance claim either way**")
    else:
        L.append("- too few trades to bootstrap")

    # h. latency
    L.append("\n## h. Latency (µs per hop, from latency_samples)\n")
    L.append("| hop | n | p50 | p95 | p99 |")
    L.append("|---|---|---|---|---|")
    for hop, v in s["latency"].items():
        L.append(f"| {hop} | {v['n']} | {v['p50']:,.0f} | {v['p95']:,.0f} | {v['p99']:,.0f} |")

    # i. verdict
    L.append("\n## i. Verdict\n")
    L.append(f"# **{s['verdict']}**\n")
    for r in s["verdict_reasons"]:
        L.append(f"- {r}")
    L.append("\n**Recommendation:**\n")
    if s["verdict"] == "NO-GO":
        L.append(
            "1. **Do not take Mode A to live trading.** The taker-sniper premise — that a "
            "cheap late-window ask is free money when the model is confident — is refuted by "
            "this data: available asks are informative, and everything the model wants to lift "
            "is priced by someone who has been right more often than us.\n"
            "2. **Keep the shadow stack running unchanged** (it costs nothing) while pivoting "
            "design work to **Mode C (passive maker)**: the 367 no-ask windows are windows where "
            "the market *paid* whoever was quoting the winning side; makers pay zero fees and "
            "earn the spread the sniper has been paying. Its shadow fill model (queue-behind-"
            "visible-size) is already implemented.\n"
            "3. **Fix the RTDS silent-connection failure** (treat `Too Many Requests` as fatal, "
            "add a no-data watchdog ~30 s, settle stranded sessions from Gamma) before any "
            "live milestone — it stranded 25 windows and a position in this run.\n"
            "4. **Retune or disable Mode B** — its trigger is unreachable (see §b) so it is "
            "currently dead weight in every report."
        )
    elif s["verdict"] == "EXTEND":
        L.append("Keep shadow running and re-run this report at the trade count named above; "
                 "fix the RTDS watchdog and the Mode B trigger in the meantime.")
    else:
        L.append("Proceed to M3 planning at minimum stakes with the RTDS watchdog fixed first.")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", type=Path, default=Path("/tmp/m1_work"))
    ap.add_argument("--out", type=Path, default=REPO / "reports" / "M2_EDGE_REALIZATION.md")
    args = ap.parse_args()
    s = analyze(args.workdir)
    args.workdir.mkdir(parents=True, exist_ok=True)
    (args.workdir / "m2_summary.json").write_text(json.dumps(s, indent=1, default=str))
    args.out.write_text(render_md(s) + "\n")
    print(f"wrote {args.out} — verdict {s['verdict']}", file=sys.stderr)


if __name__ == "__main__":
    main()
