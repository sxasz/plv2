"""M1 extraction pass: one streaming read of every raw recorder file.

Produces, in --workdir:
  feed_stats.json       per-feed frame counts, spans, top gaps, threshold
                        exceedances, per-file inventory
  chainlink_prints.csv  recv_wall_ns, oracle_ts_ms, value, src(live|backfill)
  binance_quotes.csv    recv_wall_ns, bid, ask   (bookTicker only)

The recorder keeps writing while we read; the in-progress hour file simply
ends at whatever was flushed last (a torn final line is tolerated and
counted). Nothing under data/ is written to.

Usage: m1_extract.py [--raw-dir DIR] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import csv
import heapq
import sys
import time
from pathlib import Path

import orjson

sys.path.insert(0, str(Path(__file__).parent))
from m1_common import GAP_THRESHOLDS_S, RAW_DIR, open_raw, parse_prefix, raw_files  # noqa: E402

TOP_GAPS = 10


MAX_GAP_LIST = 500


class FeedStats:
    __slots__ = ("count", "first_ns", "last_ns", "gap_heap", "over_threshold", "over_1s", "over_list")

    def __init__(self) -> None:
        self.count = 0
        self.first_ns = 0
        self.last_ns = 0
        # min-heap of (gap_ns, prev_ns, cur_ns) keeping the TOP_GAPS largest
        self.gap_heap: list[tuple[int, int, int]] = []
        self.over_threshold = 0
        self.over_1s = 0
        # every over-threshold gap, so downstream analysis (window-tail
        # overlap etc.) sees all of them, not just the top 10
        self.over_list: list[tuple[int, int, int]] = []

    def add(self, wall_ns: int, threshold_ns: int) -> None:
        if self.count == 0:
            self.first_ns = wall_ns
        else:
            gap = wall_ns - self.last_ns
            if gap > 1_000_000_000:
                self.over_1s += 1
            if gap > threshold_ns:
                self.over_threshold += 1
                if len(self.over_list) < MAX_GAP_LIST:
                    self.over_list.append((gap, self.last_ns, wall_ns))
            item = (gap, self.last_ns, wall_ns)
            if len(self.gap_heap) < TOP_GAPS:
                heapq.heappush(self.gap_heap, item)
            elif gap > self.gap_heap[0][0]:
                heapq.heapreplace(self.gap_heap, item)
        self.last_ns = wall_ns
        self.count += 1


def classify_chainlink(payload: str) -> tuple[str, list[tuple[int, float]]]:
    """Return (kind, prints). kind: live | backfill | empty | other | bad."""
    if payload == "":
        return "empty", []
    try:
        obj = orjson.loads(payload)
    except orjson.JSONDecodeError:
        return "bad", []
    inner = obj.get("payload") if isinstance(obj, dict) else None
    if isinstance(inner, dict):
        if isinstance(inner.get("data"), list):  # reconnect backfill dump (FACTS 5.8)
            out = []
            for e in inner["data"]:
                try:
                    out.append((int(e["timestamp"]), float(e["value"])))
                except (KeyError, TypeError, ValueError):
                    pass
            return "backfill", out
        if "value" in inner and "timestamp" in inner:  # live update (FACTS 5.2)
            try:
                return "live", [(int(inner["timestamp"]), float(inner["value"]))]
            except (TypeError, ValueError):
                return "bad", []
    return "other", []


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    ap.add_argument("--workdir", type=Path, required=True)
    args = ap.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)

    thresholds_ns = {k.encode(): int(v * 1e9) for k, v in GAP_THRESHOLDS_S.items()}
    stats: dict[bytes, FeedStats] = {}
    per_file: list[dict] = []
    counters = {
        "lines": 0,
        "bad_lines": 0,
        "cl_live_frames": 0,
        "cl_backfill_frames": 0,
        "cl_backfill_prints": 0,
        "cl_empty_frames": 0,
        "cl_other_frames": 0,
        "cl_bad_frames": 0,
        "bn_bookticker": 0,
        "bn_aggtrade": 0,
        "bn_other": 0,
    }

    t0 = time.time()
    cl_fh = (args.workdir / "chainlink_prints.csv").open("w", newline="")
    bn_fh = (args.workdir / "binance_quotes.csv").open("w", newline="")
    cl_csv = csv.writer(cl_fh)
    bn_csv = csv.writer(bn_fh)
    cl_csv.writerow(["recv_wall_ns", "oracle_ts_ms", "value", "src"])
    bn_csv.writerow(["recv_wall_ns", "bid", "ask"])

    for path in raw_files(args.raw_dir):
        file_counts: dict[str, int] = {}
        first_ns = last_ns = 0
        with open_raw(path) as fh:
            for line in fh:
                counters["lines"] += 1
                pre = parse_prefix(line)
                if pre is None:
                    counters["bad_lines"] += 1
                    continue
                feed, wall_ns = pre
                st = stats.get(feed)
                if st is None:
                    st = stats[feed] = FeedStats()
                st.add(wall_ns, thresholds_ns.get(feed, 10**18))
                fname = feed.decode()
                file_counts[fname] = file_counts.get(fname, 0) + 1
                if not first_ns:
                    first_ns = wall_ns
                last_ns = wall_ns

                if feed == b"rtds_chainlink":
                    doc = orjson.loads(line)
                    kind, prints = classify_chainlink(doc["payload"])
                    counters[f"cl_{kind}_frames"] += 1
                    if kind == "backfill":
                        counters["cl_backfill_prints"] += len(prints)
                    for ts_ms, value in prints:
                        cl_csv.writerow([wall_ns, ts_ms, repr(value), kind])
                elif feed == b"binance":
                    doc = orjson.loads(line)
                    try:
                        p = orjson.loads(doc["payload"])
                        stream = p.get("stream", "")
                        if stream.endswith("@bookTicker"):
                            d = p["data"]
                            bn_csv.writerow([wall_ns, d["b"], d["a"]])
                            counters["bn_bookticker"] += 1
                        elif stream.endswith("@aggTrade"):
                            counters["bn_aggtrade"] += 1
                        else:
                            counters["bn_other"] += 1
                    except (orjson.JSONDecodeError, KeyError, TypeError):
                        counters["bn_other"] += 1
        per_file.append(
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "first_wall_ns": first_ns,
                "last_wall_ns": last_ns,
                "frames": file_counts,
            }
        )
        print(f"  scanned {path.name}: {sum(file_counts.values()):,} frames", file=sys.stderr)

    cl_fh.close()
    bn_fh.close()

    out = {
        "generated_at": time.time(),
        "scan_seconds": round(time.time() - t0, 1),
        "counters": counters,
        "per_file": per_file,
        "feeds": {
            feed.decode(): {
                "count": st.count,
                "first_wall_ns": st.first_ns,
                "last_wall_ns": st.last_ns,
                "gap_threshold_s": GAP_THRESHOLDS_S.get(feed.decode()),
                "gaps_over_threshold": st.over_threshold,
                "gaps_over_1s": st.over_1s,
                "top_gaps": [
                    {"gap_s": g / 1e9, "from_wall_ns": a, "to_wall_ns": b}
                    for g, a, b in sorted(st.gap_heap, reverse=True)
                ],
                "gaps_over_threshold_list": [
                    {"gap_s": g / 1e9, "from_wall_ns": a, "to_wall_ns": b}
                    for g, a, b in st.over_list
                ],
            }
            for feed, st in stats.items()
        },
    }
    import json

    with (args.workdir / "feed_stats.json").open("w") as fh:
        json.dump(out, fh, indent=1)
    print(
        f"done: {counters['lines']:,} lines in {out['scan_seconds']}s; "
        f"feeds: { {f.decode(): s.count for f, s in stats.items()} }",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
