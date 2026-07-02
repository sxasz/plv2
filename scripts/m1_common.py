"""Shared helpers for the M1 data-quality analysis scripts.

All scripts read the recorder's raw JSONL (plain or .gz) and the bot SQLite
in read-only mode; nothing here writes into data/ — the recorder stays
untouched. Intermediates go to a --workdir chosen by the caller.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator

RAW_DIR = Path("/opt/plv2/data/raw")
DB_PATH = Path("/opt/plv2/data/bot.sqlite")
WINDOW_S = 300

# The recorder was restarted at 06:40:21 UTC on 2026-07-02 to deploy commit
# 3d645c5 (RTDS empty-frame + backfill-shape parser fixes, CLOB resubscribe
# fix). Windows that start at/after the first full boundary following the
# restart are "post-fix"; earlier ones ran on the buggy parser.
FIX_RESTART_WALL = 1782974421
POST_FIX_FIRST_WINDOW = ((FIX_RESTART_WALL // WINDOW_S) + 1) * WINDOW_S  # 1782974700

# Feed-health gap thresholds (seconds) as specified for the M1 report.
GAP_THRESHOLDS_S = {"binance": 2.0, "rtds_chainlink": 5.0, "clob_market": 10.0}

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def iso_utc(ts_s: float) -> str:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def iso_utc_s(ts_s: float) -> str:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def pctl(sorted_vals: list[float], q: float) -> float:
    """Percentile with linear interpolation on an already-sorted list."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def raw_files(raw_dir: Path = RAW_DIR) -> list[Path]:
    """Hourly raw files in chronological order (mixed .jsonl / .jsonl.gz)."""
    files: dict[int, Path] = {}
    for p in raw_dir.iterdir():
        name = p.name
        if name.endswith(".jsonl.gz"):
            hour = int(name[: -len(".jsonl.gz")])
        elif name.endswith(".jsonl"):
            hour = int(name[: -len(".jsonl")])
        else:
            continue
        # If both foo.jsonl and foo.jsonl.gz exist (mid-compression), prefer
        # the plain file: it is the complete original.
        if hour not in files or files[hour].name.endswith(".gz"):
            files[hour] = p
    return [files[h] for h in sorted(files)]


def open_raw(path: Path) -> IO[bytes]:
    if path.name.endswith(".gz"):
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return path.open("rb")


def iter_raw_lines(raw_dir: Path = RAW_DIR) -> Iterator[bytes]:
    for path in raw_files(raw_dir):
        with open_raw(path) as fh:
            yield from fh


def parse_prefix(line: bytes) -> tuple[bytes, int] | None:
    """Cheaply extract (feed, wall_ns) from a recorder line without parsing
    the (potentially multi-KB) payload. Recorder lines are orjson-serialized
    dicts with fixed key order: kind, feed, mono_ns, wall_ns, payload — so the
    first occurrences of these keys always belong to the envelope, never to
    payload content."""
    try:
        i = line.index(b'"feed":"') + 8
        j = line.index(b'"', i)
        feed = line[i:j]
        k = line.index(b'"wall_ns":', j) + 10
        m = line.index(b",", k)
        return feed, int(line[k:m])
    except ValueError:
        return None


def db_ro() -> sqlite3.Connection:
    """Read-only connection to the live WAL-mode bot database."""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)


def load_json(path: Path) -> Any:
    with path.open() as fh:
        return json.load(fh)


def dump_json(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(obj, fh, indent=1, default=str)
