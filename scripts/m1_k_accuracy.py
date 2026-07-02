"""M1 report §2 — price-to-beat (K) capture accuracy and outcome agreement.

For every completed 5-minute window:
  implied outcome = UP if close >= K else DOWN   (tie → UP, FACTS.md 1.3)
where K is the bot's captured price-to-beat (k_captures table) and close is
the first Chainlink print at/after the window-end boundary reconstructed from
the raw recording (live + reconnect-backfill prints, deduplicated).

The implied outcome is compared against the market's actual resolution from
the Gamma API (events?slug=btc-updown-5m-{window_start}). Gamma responses are
cached in the workdir; only unresolved entries are refetched.

Usage: m1_k_accuracy.py --workdir DIR [--offline]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m1_common import (
    GAMMA_EVENTS_URL,
    POST_FIX_FIRST_WINDOW,
    WINDOW_S,
    db_ro,
    dump_json,
    iso_utc,
    load_json,
    pctl,
)

CLOSE_SEARCH_LIMIT_MS = 120_000  # a boundary print later than this ⇒ treat close as missing
K_RAW_VALID_MS = (
    5_000  # archive "knows" the true K only if a print exists this close to the boundary
)


def load_prints(workdir: Path) -> list[tuple[int, float]]:
    """Deduplicated (oracle_ts_ms, value) ascending. Live prints win over
    backfill duplicates; conflicting values for the same oracle second are
    counted and reported."""
    best: dict[int, tuple[int, float, str]] = {}
    conflicts = 0
    with (workdir / "chainlink_prints.csv").open() as fh:
        for row in csv.DictReader(fh):
            ts = int(row["oracle_ts_ms"])
            val = float(row["value"])
            src = row["src"]
            cur = best.get(ts)
            if cur is None:
                best[ts] = (int(row["recv_wall_ns"]), val, src)
            else:
                if val != cur[1]:
                    conflicts += 1
                if src == "live" and cur[2] != "live":
                    best[ts] = (int(row["recv_wall_ns"]), val, src)
    if conflicts:
        print(f"WARNING: {conflicts} same-timestamp value conflicts in prints", file=sys.stderr)
    return sorted((ts, v) for ts, (_, v, _) in best.items())


def first_print_at_or_after(
    prints: list[tuple[int, float]], ts_ms: int
) -> tuple[int, float] | None:
    import bisect

    i = bisect.bisect_left(prints, (ts_ms, float("-inf")))
    if i < len(prints) and prints[i][0] - ts_ms <= CLOSE_SEARCH_LIMIT_MS:
        return prints[i]
    return None


def fetch_gamma(slug: str) -> dict | None:
    req = urllib.request.Request(
        f"{GAMMA_EVENTS_URL}?slug={slug}", headers={"User-Agent": "plv2-m1-analysis/1.0"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                events = json.load(resp)
            break
        except Exception as e:
            if attempt == 2:
                print(f"gamma fetch failed for {slug}: {e}", file=sys.stderr)
                return None
            time.sleep(1.0 + attempt)
    if not events:
        return None
    markets = events[0].get("markets") or []
    market = next((m for m in markets if m.get("slug") == slug), markets[0] if markets else None)
    if market is None:
        return None
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(market.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        outcomes, prices = [], []
    winner = None
    if outcomes and prices and len(outcomes) == len(prices) and max(prices) > 0.99:
        winner = outcomes[prices.index(max(prices))].upper()
    return {
        "slug": slug,
        "closed": bool(market.get("closed")),
        "winner": winner,  # "UP" / "DOWN" / None if unresolved
        "uma_status": market.get("umaResolutionStatus"),
        "closed_time": market.get("closedTime"),
        "condition_id": market.get("conditionId"),
    }


def analyze(workdir: Path, offline: bool = False) -> tuple[dict, str]:
    prints = load_prints(workdir)
    conn = db_ro()
    kcaps = {
        r[0]: {"k": r[1], "oracle_ts_ms": r[2], "recv_wall_ns": r[3], "lag_ms": r[4]}
        for r in conn.execute(
            "SELECT window_start, k, oracle_ts_ms, recv_wall_ns, lag_ms FROM k_captures"
        )
    }
    engine_windows = {
        r[0]: {"outcome": r[1], "close_price": r[2]}
        for r in conn.execute("SELECT window_start, outcome, close_price FROM windows")
    }
    conn.close()

    if not kcaps:
        raise SystemExit("k_captures is empty — nothing to analyze")
    first_recv_s = min(k["recv_wall_ns"] for k in kcaps.values()) / 1e9
    # Boundaries the recorder could have captured: from the first 5-minute
    # boundary after it started receiving, through the latest captured one.
    first_boundary = int(-(-first_recv_s // WINDOW_S)) * WINDOW_S
    expected = list(range(first_boundary, max(kcaps) + 1, WINDOW_S))
    captured = [b for b in expected if b in kcaps]
    missed = [b for b in expected if b not in kcaps]
    extra_startup = sorted(set(kcaps) - set(expected))

    lags_all = sorted(k["lag_ms"] for k in kcaps.values())
    lags_post = sorted(k["lag_ms"] for w, k in kcaps.items() if w >= POST_FIX_FIRST_WINDOW)
    # Receive-time lag: when the bot actually KNEW K, relative to the boundary
    # (oracle_ts lag above measures only the oracle-side stamp).
    recv_lags_post = sorted(
        k["recv_wall_ns"] / 1e6 - w * 1000 for w, k in kcaps.items() if w >= POST_FIX_FIRST_WINDOW
    )

    # Gamma resolutions (cached).
    cache_path = workdir / "gamma_cache.json"
    cache: dict[str, dict] = load_json(cache_path) if cache_path.exists() else {}
    completed = sorted(w for w in kcaps if w + WINDOW_S <= time.time())
    for w in completed:
        slug = f"btc-updown-5m-{w}"
        entry = cache.get(slug)
        if entry is None or (entry.get("winner") is None and not offline):
            if offline:
                continue
            got = fetch_gamma(slug)
            if got is not None:
                cache[slug] = got
            time.sleep(0.15)
    dump_json(cache, cache_path)

    rows = []
    agree = disagree = unresolved = no_close = 0
    for w in completed:
        kc = kcaps[w]
        k_table = kc["k"]
        raw_k = first_print_at_or_after(prints, w * 1000)
        raw_close = first_print_at_or_after(prints, (w + WINDOW_S) * 1000)
        gamma = cache.get(f"btc-updown-5m-{w}", {})
        eng = engine_windows.get(w, {})
        k_raw_delay_ms = raw_k[0] - w * 1000 if raw_k else None
        k_raw_valid = raw_k is not None and k_raw_delay_ms <= K_RAW_VALID_MS
        row = {
            "window_start": w,
            "utc": iso_utc(w),
            "k_table": k_table,
            "k_lag_ms": kc["lag_ms"],
            "k_raw": raw_k[1] if raw_k else None,
            "k_raw_delay_ms": k_raw_delay_ms,
            "k_raw_valid": k_raw_valid,
            "k_raw_matches": (k_raw_valid and abs(raw_k[1] - k_table) < 1e-6),
            "close_raw": raw_close[1] if raw_close else None,
            "close_oracle_ts_ms": raw_close[0] if raw_close else None,
            "engine_outcome": eng.get("outcome"),
            "engine_close": eng.get("close_price"),
            "gamma_winner": gamma.get("winner"),
            "prefix_window": w < POST_FIX_FIRST_WINDOW,
        }
        if raw_close is None:
            row["implied"] = None
            no_close += 1
        else:
            row["implied"] = "UP" if raw_close[1] >= k_table else "DOWN"
            row["margin_usd"] = raw_close[1] - k_table
        # Archive view: outcome implied by the recorded archive alone
        # (backfill-recovered boundary print as K), independent of what the
        # live engine believed at the time.
        row["implied_raw"] = (
            ("UP" if raw_close[1] >= raw_k[1] else "DOWN")
            if (k_raw_valid and raw_close is not None)
            else None
        )
        if row["implied"] is None or row["gamma_winner"] is None:
            # count each excluded window once, preferring the data-side reason
            if row["implied"] is None:
                pass  # already counted in no_close
            elif row["gamma_winner"] is None:
                unresolved += 1
            row["status"] = "excluded"
        elif row["implied"] == row["gamma_winner"]:
            agree += 1
            row["status"] = "agree"
        else:
            disagree += 1
            row["status"] = "DISAGREE"
        rows.append(row)

    n_compared = agree + disagree
    agreement_pct = 100.0 * agree / n_compared if n_compared else float("nan")
    raw_agree = sum(1 for r in rows if r["implied_raw"] and r["implied_raw"] == r["gamma_winner"])
    raw_compared = sum(1 for r in rows if r["implied_raw"] and r["gamma_winner"])
    post_rows = [r for r in rows if not r["prefix_window"] and r["status"] in ("agree", "DISAGREE")]
    post_agree = sum(1 for r in post_rows if r["status"] == "agree")

    summary = {
        "boundaries_expected": len(expected),
        "boundaries_captured": len(captured),
        "boundaries_missed": missed,
        "startup_partial_windows": extra_startup,
        "lag_ms_all": {
            "n": len(lags_all),
            "median": pctl(lags_all, 0.5),
            "p95": pctl(lags_all, 0.95),
            "max": max(lags_all) if lags_all else None,
        },
        "lag_ms_post_fix": {
            "n": len(lags_post),
            "median": pctl(lags_post, 0.5),
            "p95": pctl(lags_post, 0.95),
            "max": max(lags_post) if lags_post else None,
        },
        "recv_lag_ms_post_fix": {
            "n": len(recv_lags_post),
            "median": pctl(recv_lags_post, 0.5),
            "p95": pctl(recv_lags_post, 0.95),
            "max": max(recv_lags_post) if recv_lags_post else None,
        },
        "windows_compared": n_compared,
        "agree": agree,
        "disagree": disagree,
        "post_fix_compared": len(post_rows),
        "post_fix_agree": post_agree,
        "archive_compared": raw_compared,
        "archive_agree": raw_agree,
        "unresolved_on_gamma": unresolved,
        "missing_close_in_raw": no_close,
        "agreement_pct": agreement_pct,
        "k_raw_mismatches": [r for r in rows if r["k_raw_valid"] and not r["k_raw_matches"]],
        "k_raw_unknowable": [r for r in rows if not r["k_raw_valid"]],
        "rows": rows,
    }

    # ---- markdown ----
    L: list[str] = []
    L.append("## 2. Price-to-beat (K) accuracy — the killer test\n")
    startup_note = ""
    if extra_startup:
        ts = ", ".join(iso_utc(w) for w in extra_startup)
        startup_note = (
            f" In addition the partial startup window ({ts}) — already running when the "
            "recorder came up — got a late K and is analyzed below but excluded from coverage."
        )
    L.append(
        f"**K coverage:** {len(captured)}/{len(expected)} 5-minute boundaries inside the "
        f"recording span got a K captured.{startup_note}"
    )
    if missed:
        L.append(f"\n**MISSED boundaries:** {[iso_utc(b) for b in missed]}")
    la, lp, rp = summary["lag_ms_all"], summary["lag_ms_post_fix"], summary["recv_lag_ms_post_fix"]
    L.append("\n**K capture lag:**\n")
    L.append("| population | n | median | p95 | max |")
    L.append("|---|---|---|---|---|")
    L.append(
        f"| oracle stamp − boundary, all windows | {la['n']} | {la['median']:.0f} ms | "
        f"{la['p95']:.0f} ms | {la['max']:.0f} ms |"
    )
    L.append(
        f"| oracle stamp − boundary, post-fix | {lp['n']} | {lp['median']:.0f} ms | "
        f"{lp['p95']:.0f} ms | {lp['max']:.0f} ms |"
    )
    L.append(
        f"| received-by-bot − boundary, post-fix | {rp['n']} | {rp['median']:.0f} ms | "
        f"{rp['p95']:.0f} ms | {rp['max']:.0f} ms |"
    )
    L.append(
        f"\nPost-fix = windows from {iso_utc(POST_FIX_FIRST_WINDOW)} on; earlier windows ran the "
        "RTDS parser bugs fixed in `3d645c5` (deployed 06:40:21Z) and are quarantined above "
        "rather than mixed into the health signal. The received-by-bot row is the operative "
        "number: how long after the boundary the strategy actually knows the price to beat."
    )
    mism = summary["k_raw_mismatches"]
    if mism:
        L.append(
            f"\n**K cross-check:** {len(mism)} window(s) where the bot's captured K differs from "
            "the true boundary print reconstructed from the raw archive (live capture was late "
            "or wrong; reconnect backfill recovered the real print):\n"
        )
        L.append(
            "| window (UTC) | K table (lag ms) | K archive (print at boundary +ms) | Δ | pre-fix? |"
        )
        L.append("|---|---|---|---|---|")
        for r in mism:
            L.append(
                f"| {r['utc']} | {r['k_table']:.2f} ({r['k_lag_ms']:.0f}) | {r['k_raw']:.2f} "
                f"(+{r['k_raw_delay_ms']:.0f}) | {r['k_table'] - r['k_raw']:+.2f} | "
                f"{'yes' if r['prefix_window'] else 'no'} |"
            )
    else:
        L.append(
            "\n**K cross-check:** every captured K exactly equals the first at-/after-boundary "
            "print in the raw archive."
        )
    for r in summary["k_raw_unknowable"]:
        L.append(
            f"\n(For {r['utc']} the archive itself has no print near the boundary — first print "
            f"{(r['k_raw_delay_ms'] or 0) / 1000:.0f}s after it — recording started mid-window, "
            "so no ground-truth K exists for it.)"
        )

    L.append(
        f"\n**Outcome agreement vs Gamma (killer test):** {n_compared} resolved windows compared."
        f"\n\n| view | agree | compared | rate |"
        f"\n|---|---|---|---|"
        f"\n| bot's live K (what the engine believed) | {agree} | {n_compared} | "
        f"**{agreement_pct:.1f}%** |"
        f"\n| post-fix windows only, bot's live K | {summary['post_fix_agree']} | "
        f"{summary['post_fix_compared']} | "
        f"**{100.0 * summary['post_fix_agree'] / summary['post_fix_compared']:.1f}%** |"
        f"\n| recorded archive (backfill-recovered K) | {summary['archive_agree']} | "
        f"{summary['archive_compared']} | "
        f"**{100.0 * summary['archive_agree'] / summary['archive_compared']:.1f}%** |"
        f"\n\n({no_close} window(s) excluded for having no boundary close print in the archive "
        f"yet, a further {unresolved} for not being resolved on Gamma yet.)"
    )
    dis = [r for r in rows if r["status"] == "DISAGREE"]
    if dis:
        L.append("\n**Every disagreement (bot-view):**\n")
        L.append(
            "| window (UTC) | K (lag ms) | close | margin $ | implied | archive-implied | "
            "Gamma | pre-fix? |"
        )
        L.append("|---|---|---|---|---|---|---|---|")
        for r in dis:
            L.append(
                f"| {r['utc']} | {r['k_table']:.2f} ({r['k_lag_ms']:.0f}) | {r['close_raw']:.2f} | "
                f"{r['margin_usd']:+.2f} | {r['implied']} | {r['implied_raw']} | "
                f"{r['gamma_winner']} | {'yes' if r['prefix_window'] else 'no'} |"
            )
    excl = [r for r in rows if r["status"] == "excluded"]
    if excl:
        L.append(
            "\nExcluded windows: "
            + ", ".join(
                f"{r['utc']} ({'no close print in archive yet' if r['implied'] is None else 'no Gamma resolution yet'})"
                for r in excl
            )
        )

    return summary, "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--offline", action="store_true", help="use only cached Gamma responses")
    args = ap.parse_args()
    summary, md = analyze(args.workdir, offline=args.offline)
    dump_json(summary, args.workdir / "k_accuracy.json")
    print(md)


if __name__ == "__main__":
    main()
