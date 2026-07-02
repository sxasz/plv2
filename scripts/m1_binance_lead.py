"""M1 report §4 — does Binance lead the Chainlink oracle, and by how much?

Method: both series are sampled last-observation-carried-forward onto a
100 ms grid using LOCAL RECEIVE times (that is the tradeable signal: when we
saw Binance move vs when we saw the oracle move — transport latency included
on both legs). 1-second log returns are computed on the grid, and the Pearson
correlation of binance_return(t − lag) vs chainlink_return(t) is evaluated
for lags 0..5 s. Grid points where either series is stale (> 10 s without an
update: recorder restart, WS reconnects) are excluded.

Basis = chainlink_value − binance_mid at each live oracle print's receive
time (mean/std), i.e. the level offset the fair-value model must carry.

Only live prints are used (backfill arrival times are batch artifacts).

Usage: m1_binance_lead.py --workdir DIR
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m1_common import dump_json, pctl  # noqa: E402

GRID_MS = 100
RET_STEPS = 10  # 1 s returns on the 100 ms grid
MAX_LAG_STEPS = 50  # 5 s
STALE_NS = 10 * 10**9


def locf_grid(times_ns: array, values: array, grid_start_ns: int, n: int) -> list[float]:
    """Sample (times, values) onto the grid; NaN where no fresh (<10 s) obs."""
    out = [math.nan] * n
    j = 0
    m = len(times_ns)
    step = GRID_MS * 10**6
    for i in range(n):
        t = grid_start_ns + i * step
        while j < m and times_ns[j] <= t:
            j += 1
        if j > 0 and t - times_ns[j - 1] <= STALE_NS:
            out[i] = values[j - 1]
    return out


def log_returns(series: list[float]) -> list[float]:
    n = len(series)
    out = [math.nan] * n
    for i in range(RET_STEPS, n):
        a, b = series[i - RET_STEPS], series[i]
        if not (math.isnan(a) or math.isnan(b)) and a > 0 and b > 0:
            out[i] = math.log(b / a)
    return out


def pearson(xs: list[float], ys: list[float]) -> tuple[float, int]:
    n = 0
    sx = sy = sxx = syy = sxy = 0.0
    for x, y in zip(xs, ys):
        if math.isnan(x) or math.isnan(y):
            continue
        n += 1
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
    if n < 100:
        return math.nan, n
    cov = sxy - sx * sy / n
    vx = sxx - sx * sx / n
    vy = syy - sy * sy / n
    if vx <= 0 or vy <= 0:
        return math.nan, n
    return cov / math.sqrt(vx * vy), n


def analyze(workdir: Path) -> tuple[dict, str]:
    bn_t: array = array("q")
    bn_mid: array = array("d")
    with (workdir / "binance_quotes.csv").open() as fh:
        rd = csv.reader(fh)
        next(rd)
        for t, b, a in rd:
            bn_t.append(int(t))
            bn_mid.append((float(b) + float(a)) / 2.0)

    cl_t: array = array("q")
    cl_v: array = array("d")
    with (workdir / "chainlink_prints.csv").open() as fh:
        for row in csv.DictReader(fh):
            if row["src"] == "live":
                cl_t.append(int(row["recv_wall_ns"]))
                cl_v.append(float(row["value"]))

    start_ns = max(bn_t[0], cl_t[0])
    end_ns = min(bn_t[-1], cl_t[-1])
    n = int((end_ns - start_ns) // (GRID_MS * 10**6))
    bn_g = locf_grid(bn_t, bn_mid, start_ns, n)
    cl_g = locf_grid(cl_t, cl_v, start_ns, n)
    bn_r = log_returns(bn_g)
    cl_r = log_returns(cl_g)

    corr_by_lag: list[dict] = []
    for k in range(MAX_LAG_STEPS + 1):
        # binance shifted k steps into the past vs chainlink now
        c, npairs = pearson(bn_r[: len(bn_r) - k] if k else bn_r, cl_r[k:])
        corr_by_lag.append({"lag_ms": k * GRID_MS, "corr": c, "n": npairs})
    best = max((e for e in corr_by_lag if not math.isnan(e["corr"])), key=lambda e: e["corr"])

    # Basis at live print receive times.
    basis: list[float] = []
    j = 0
    for t, v in zip(cl_t, cl_v):
        while j < len(bn_t) and bn_t[j] <= t:
            j += 1
        if j > 0 and t - bn_t[j - 1] <= STALE_NS:
            basis.append(v - bn_mid[j - 1])
    basis_sorted = sorted(basis)
    bmean = sum(basis) / len(basis)
    bstd = math.sqrt(sum((x - bmean) ** 2 for x in basis) / (len(basis) - 1))

    summary = {
        "grid_ms": GRID_MS,
        "return_horizon_ms": RET_STEPS * GRID_MS,
        "grid_points": n,
        "best_lag_ms": best["lag_ms"],
        "best_corr": best["corr"],
        "corr_at_0": corr_by_lag[0]["corr"],
        "corr_by_lag": corr_by_lag,
        "basis_usd": {
            "n": len(basis),
            "mean": bmean,
            "std": bstd,
            "p5": pctl(basis_sorted, 0.05),
            "p95": pctl(basis_sorted, 0.95),
        },
    }

    b = summary["basis_usd"]
    L = [
        "## 4. Binance lead over the oracle\n",
        f"1-second log returns on a {GRID_MS} ms LOCF grid over {n:,} grid points "
        "(local receive clocks, stale/reconnect stretches masked).\n",
        f"**Maximum cross-correlation: {best['corr']:.3f} at lag {best['lag_ms']} ms** "
        f"(Binance leading; zero-lag correlation {corr_by_lag[0]['corr']:.3f}).\n",
        "| lag (ms) | corr |",
        "|---|---|",
    ]
    for e in corr_by_lag:
        if e["lag_ms"] % 500 == 0 or e["lag_ms"] == best["lag_ms"]:
            mark = " ← max" if e["lag_ms"] == best["lag_ms"] else ""
            L.append(f"| {e['lag_ms']} | {e['corr']:.3f}{mark} |")
    L += [
        "",
        f"**Basis (chainlink − binance mid)** at {b['n']:,} live prints: mean "
        f"**{b['mean']:+.2f} USD**, std **{b['std']:.2f}**, p5 {b['p5']:+.2f}, p95 {b['p95']:+.2f}. "
        f"The level (~{b['mean'] / 60000 * 1e4:.0f} bp) is expected: Binance quotes BTC/**USDT** "
        "while the oracle is BTC/**USD**, so the mean basis embeds the USDT/USD rate plus "
        "transport asymmetry. The model's `basis_window_s` EWMA carries the level; the std is "
        "what matters for edge sizing.",
    ]
    return summary, "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", type=Path, required=True)
    args = ap.parse_args()
    summary, md = analyze(args.workdir)
    dump_json(summary, args.workdir / "binance_lead.json")
    print(md)


if __name__ == "__main__":
    main()
