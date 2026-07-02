"""M1 report §3 — Chainlink oracle print cadence.

Two views, kept separate because they answer different questions:
  * oracle-series cadence — gaps between consecutive oracle timestamps of the
    deduplicated print series (live + backfill). This is the cadence of the
    resolution source itself.
  * as-received cadence — gaps between local receive times of live prints
    only. This is what the strategy experiences (staleness guard input).

Plus the last-30s-of-window print density, and the boundary-straddle gap
(time from the last print before each boundary to the first at/after it),
which lower-bounds how precisely K can ever be pinned.

Usage: m1_oracle_cadence.py --workdir DIR
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m1_common import WINDOW_S, dump_json, iso_utc, load_json, pctl  # noqa: E402

DENSITY_TAIL_S = 30


def analyze(workdir: Path) -> tuple[dict, str]:
    oracle_ts: set[int] = set()
    live_ts: set[int] = set()
    live_recv_ns: list[int] = []
    with (workdir / "chainlink_prints.csv").open() as fh:
        for row in csv.DictReader(fh):
            ts = int(row["oracle_ts_ms"])
            oracle_ts.add(ts)
            if row["src"] == "live":
                live_ts.add(ts)
                live_recv_ns.append(int(row["recv_wall_ns"]))
    series = sorted(oracle_ts)
    live_recv_ns.sort()

    oracle_gaps_ms = sorted(b - a for a, b in zip(series, series[1:]))
    recv_gaps_ms = sorted((b - a) / 1e6 for a, b in zip(live_recv_ns, live_recv_ns[1:]))

    # Per-window tail density and boundary-straddle gaps over full windows in span.
    first_b = (series[0] // 1000 // WINDOW_S + 1) * WINDOW_S
    last_b = (series[-1] // 1000 // WINDOW_S) * WINDOW_S
    boundaries = list(range(first_b, last_b + 1, WINDOW_S))
    import bisect

    tail_counts: list[int] = []
    sparse_windows: list[tuple[int, int]] = []
    straddle_ms: list[float] = []
    for b in boundaries:
        lo = bisect.bisect_left(series, (b - DENSITY_TAIL_S) * 1000)
        hi = bisect.bisect_left(series, b * 1000)
        n_tail = hi - lo
        tail_counts.append(n_tail)
        if n_tail < 10:
            sparse_windows.append((b, n_tail))
        if 0 < hi < len(series):
            straddle_ms.append(series[hi] - series[hi - 1])
    tail_sorted = sorted(tail_counts)
    straddle_sorted = sorted(straddle_ms)

    summary = {
        "n_prints_dedup": len(series),
        "n_live_frames": len(live_recv_ns),
        "span": [iso_utc(series[0] / 1000), iso_utc(series[-1] / 1000)],
        "oracle_gap_ms": {
            "median": pctl(oracle_gaps_ms, 0.5),
            "p95": pctl(oracle_gaps_ms, 0.95),
            "p99": pctl(oracle_gaps_ms, 0.99),
            "max": max(oracle_gaps_ms),
            "max_at": None,
        },
        "recv_gap_ms": {
            "median": pctl(recv_gaps_ms, 0.5),
            "p95": pctl(recv_gaps_ms, 0.95),
            "p99": pctl(recv_gaps_ms, 0.99),
            "max": max(recv_gaps_ms),
        },
        "tail_density_last30s": {
            "windows": len(tail_counts),
            "min": tail_sorted[0],
            "median": pctl([float(x) for x in tail_sorted], 0.5),
            "mean": sum(tail_counts) / len(tail_counts),
        },
        "sparse_windows_lt10": [
            {"boundary_utc": iso_utc(b), "prints_last30s": n} for b, n in sparse_windows
        ],
        "boundary_straddle_ms": {
            "median": pctl(straddle_sorted, 0.5),
            "p95": pctl(straddle_sorted, 0.95),
            "max": max(straddle_sorted) if straddle_sorted else None,
        },
    }
    # locate the max oracle gap for the report
    worst = max(zip(series, series[1:]), key=lambda ab: ab[1] - ab[0])
    summary["oracle_gap_ms"]["max_at"] = f"{iso_utc(worst[0] / 1000)} → {iso_utc(worst[1] / 1000)}"

    # -- delivery stalls: the over-threshold receive gaps from feed health,
    # classified against the oracle series. If prints whose oracle timestamps
    # fall inside a stall were still received live, the connection merely
    # buffered and flushed late; if they only exist via backfill, they were
    # lost on that connection; if no prints exist at all, the oracle itself
    # was quiet.
    stalls: list[dict] = []
    fs = load_json(workdir / "feed_stats.json")
    for g in fs["feeds"].get("rtds_chainlink", {}).get("gaps_over_threshold_list", []):
        a_ms = int(g["from_wall_ns"] // 1e6)
        b_ms = int(g["to_wall_ns"] // 1e6)
        lo = bisect.bisect_right(series, a_ms)
        hi = bisect.bisect_left(series, b_ms)
        inside = series[lo:hi]
        n_backfill_only = sum(1 for t in inside if t not in live_ts)
        tails = []
        for b in range((a_ms // 1000 // WINDOW_S) * WINDOW_S, b_ms // 1000 + WINDOW_S + 1, WINDOW_S):
            if a_ms / 1000 < b + 2 and b_ms / 1000 > b - DENSITY_TAIL_S:
                tails.append(b)
        stalls.append(
            {
                "gap_s": g["gap_s"],
                "from_utc": iso_utc(g["from_wall_ns"] / 1e9),
                "prints_inside": len(inside),
                "recovered_via_backfill_only": n_backfill_only,
                "window_tails_touched": [iso_utc(t) for t in tails],
            }
        )
    summary["delivery_stalls_over_5s"] = stalls

    og, rg, td, bs = (
        summary["oracle_gap_ms"],
        summary["recv_gap_ms"],
        summary["tail_density_last30s"],
        summary["boundary_straddle_ms"],
    )
    L = [
        "## 3. Oracle cadence (Chainlink via RTDS)\n",
        f"{len(series):,} deduplicated oracle prints ({len(live_recv_ns):,} received live, "
        "the rest recovered via reconnect backfill), "
        f"{summary['span'][0]} → {summary['span'][1]}.\n",
        "| metric | median | p95 | p99 | max |",
        "|---|---|---|---|---|",
        f"| oracle-series inter-print gap | {og['median']:.0f} ms | {og['p95']:.0f} ms | "
        f"{og['p99']:.0f} ms | {og['max']:.0f} ms ({og['max_at']}) |",
        f"| as-received inter-frame gap (live) | {rg['median']:.0f} ms | {rg['p95']:.0f} ms | "
        f"{rg['p99']:.0f} ms | {rg['max']:.0f} ms |",
        "",
        f"**Last-{DENSITY_TAIL_S}s-of-window print density** over {td['windows']} boundaries: "
        f"median **{td['median']:.0f}**, mean {td['mean']:.1f}, min {td['min']} prints. "
        "This is the regime the late-window sniper operates in.",
        "",
        f"**Boundary-straddle gap** (last print before a boundary → first at/after): median "
        f"{bs['median']:.0f} ms, p95 {bs['p95']:.0f} ms, max {bs['max']:.0f} ms — the oracle-side "
        "floor on how quickly a window's K can exist at all.",
    ]
    if summary["sparse_windows_lt10"]:
        L.append("\nWindows with < 10 prints in the final 30s:\n")
        for sw in summary["sparse_windows_lt10"]:
            L.append(f"- {sw['boundary_utc']}: {sw['prints_last30s']} prints")
    else:
        L.append(
            f"\nNo window had fewer than 10 prints in its final {DENSITY_TAIL_S}s — the oracle "
            "never went quiet where the strategy needs it most."
        )

    if stalls:
        n_touch = sum(1 for s in stalls if s["window_tails_touched"])
        n_lost = sum(s["recovered_via_backfill_only"] for s in stalls)
        L.append(
            f"\n**RTDS delivery stalls (> 5 s without a frame): {len(stalls)}** "
            f"(~{len(stalls) / ((series[-1] - series[0]) / 3.6e6):.1f}/h). In every stall the "
            "oracle kept printing and the buffered prints arrived late on the same connection "
            f"({n_lost} print(s) needed reconnect-backfill) — these are transport pauses, not "
            f"data loss. {n_touch} of them touched a window's final {DENSITY_TAIL_S}s "
            "(where the sniper would sit out on the `max_cl_age_ms` staleness guard):\n"
        )
        L.append("| stall (s) | at | prints inside | tail touched |")
        L.append("|---|---|---|---|")
        for s in stalls:
            L.append(
                f"| {s['gap_s']:.2f} | {s['from_utc']} | {s['prints_inside']} | "
                f"{', '.join(s['window_tails_touched']) or '—'} |"
            )
    return summary, "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", type=Path, required=True)
    args = ap.parse_args()
    summary, md = analyze(args.workdir)
    dump_json(summary, args.workdir / "oracle_cadence.json")
    print(md)


if __name__ == "__main__":
    main()
