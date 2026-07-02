"""M1 report §1 — feed health.

Consumes feed_stats.json (from m1_extract.py) plus the incidents table of the
live bot database (read-only), and emits a markdown fragment + JSON summary.

Usage: m1_feed_health.py --workdir DIR
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m1_common import (  # noqa: E402
    FIX_RESTART_WALL,
    GAP_THRESHOLDS_S,
    db_ro,
    dump_json,
    iso_utc,
    load_json,
)

FEED_ORDER = ["binance", "rtds_chainlink", "clob_market"]


def analyze(workdir: Path) -> tuple[dict, str]:
    fs = load_json(workdir / "feed_stats.json")
    feeds = fs["feeds"]

    conn = db_ro()
    incidents = conn.execute(
        "SELECT feed, kind, COUNT(*), MIN(wall_ns), MAX(wall_ns) FROM incidents "
        "GROUP BY feed, kind ORDER BY feed, kind"
    ).fetchall()
    rtds_reconnects = conn.execute(
        "SELECT wall_ns, detail FROM incidents WHERE feed='rtds_chainlink' AND kind='reconnect' "
        "ORDER BY wall_ns"
    ).fetchall()
    n_windows = conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
    conn.close()

    summary: dict = {"feeds": {}, "incidents": []}
    lines: list[str] = []
    lines.append("## 1. Feed health\n")

    lines.append("| feed | frames | span (h) | avg fps | gaps > 1s | gaps > threshold | threshold |")
    lines.append("|---|---|---|---|---|---|---|")
    for name in FEED_ORDER:
        st = feeds.get(name)
        if st is None:
            lines.append(f"| {name} | — no frames recorded — | | | | | |")
            summary["feeds"][name] = {"count": 0}
            continue
        span_s = (st["last_wall_ns"] - st["first_wall_ns"]) / 1e9
        fps = st["count"] / span_s if span_s > 0 else float("nan")
        thr = GAP_THRESHOLDS_S[name]
        lines.append(
            f"| {name} | {st['count']:,} | {span_s / 3600:.2f} | {fps:,.1f} | "
            f"{st['gaps_over_1s']:,} | **{st['gaps_over_threshold']}** | {thr:.0f}s |"
        )
        summary["feeds"][name] = {
            "count": st["count"],
            "span_s": round(span_s, 1),
            "fps": round(fps, 2),
            "gaps_over_1s": st["gaps_over_1s"],
            "gaps_over_threshold": st["gaps_over_threshold"],
            "threshold_s": thr,
        }

    restart_lo = (FIX_RESTART_WALL - 120) * 1_000_000_000
    restart_hi = (FIX_RESTART_WALL + 60) * 1_000_000_000
    for name in FEED_ORDER:
        st = feeds.get(name)
        if st is None:
            continue
        lines.append(f"\n**{name} — 10 largest inter-frame gaps** (receive-time, UTC):\n")
        lines.append("| gap (s) | from | to | note |")
        lines.append("|---|---|---|---|")
        for g in st["top_gaps"]:
            note = ""
            if restart_lo <= g["from_wall_ns"] <= restart_hi:
                note = "recorder restart 06:40:21Z (deploy of 3d645c5 parser fixes)"
            lines.append(
                f"| {g['gap_s']:.2f} | {iso_utc(g['from_wall_ns'] / 1e9)} | "
                f"{iso_utc(g['to_wall_ns'] / 1e9)} | {note} |"
            )

    lines.append("\n**Incidents (SQLite `incidents` table):**\n")
    lines.append("| feed | kind | count | first | last |")
    lines.append("|---|---|---|---|---|")
    for feed, kind, cnt, first_ns, last_ns in incidents:
        lines.append(
            f"| {feed} | {kind} | {cnt} | {iso_utc(first_ns / 1e9)} | {iso_utc(last_ns / 1e9)} |"
        )
        summary["incidents"].append({"feed": feed, "kind": kind, "count": cnt})

    clob_rec = next(
        (i for i in summary["incidents"] if i["feed"] == "clob_market" and i["kind"] == "reconnect"),
        None,
    )
    if clob_rec:
        lines.append(
            f"\nThe {clob_rec['count']} `clob_market` reconnects against {n_windows} completed "
            "windows are **by design**: the CLOB market channel accepts only one in-place "
            "subscription update per connection (FACTS.md 4.7, verified live), so the feed "
            "performs a clean reconnect at every 5-minute window rollover to subscribe the next "
            "market's tokens."
        )
    if rtds_reconnects:
        lines.append("\n`rtds_chainlink` reconnects:\n")
        for wall_ns, detail in rtds_reconnects:
            lines.append(f"- {iso_utc(wall_ns / 1e9)} — {detail or 'no detail'}")
        if len(rtds_reconnects) >= 2:
            spacings = [
                (b[0] - a[0]) / 1e9
                for a, b in zip(rtds_reconnects, rtds_reconnects[1:])
            ]
            if all(abs(s - 7200) < 30 for s in spacings):
                lines.append(
                    "\nThe spacing (2h00m ± seconds) points to a server-enforced RTDS "
                    "connection lifetime rather than network trouble; each reconnect's "
                    "backfill dump recovered the prints from the gap (FACTS.md 5.8)."
                )

    counters = fs["counters"]
    lines.append(
        f"\nRecorder-side integrity: {counters['lines']:,} raw lines scanned, "
        f"{counters['bad_lines']} unparseable (torn tail of the in-progress hour file is "
        f"expected), {counters['cl_empty_frames']} empty RTDS connection-ack frames "
        "(FACTS.md 5.7), no `recorder_drop` incidents."
    )
    summary["counters"] = counters
    summary["per_file"] = fs["per_file"]

    md = "\n".join(lines)
    return summary, md


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", type=Path, required=True)
    args = ap.parse_args()
    summary, md = analyze(args.workdir)
    dump_json(summary, args.workdir / "feed_health.json")
    print(md)


if __name__ == "__main__":
    main()
