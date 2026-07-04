"""Assemble reports/M1_DATA_QUALITY.md from the four M1 analyzers.

Run m1_extract.py first (single pass over data/raw), then this script:

    .venv/bin/python scripts/m1_extract.py --workdir /tmp/m1_work
    .venv/bin/python scripts/m1_report.py  --workdir /tmp/m1_work

The verdict is rule-based and printed with its reasons; thresholds are
documented inline where they are checked.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import m1_binance_lead
import m1_feed_health
import m1_k_accuracy
import m1_oracle_cadence
from m1_common import POST_FIX_FIRST_WINDOW, dump_json, iso_utc, iso_utc_s


def verdict(fh: dict, ka: dict, oc: dict, bl: dict) -> tuple[str, list[str], list[str]]:
    """Rule-based verdict. Three tiers:
    blocking → NO-GO, open_questions → INVESTIGATE, conditions → GO with notes.
    """
    blocking: list[str] = []
    investigate: list[str] = []
    conditions: list[str] = []
    good: list[str] = []

    # -- outcome agreement: the one non-negotiable -----------------------------
    dis = [r for r in ka["rows"] if r["status"] == "DISAGREE"]
    dis_postfix = [r for r in dis if not r["prefix_window"]]
    archive_clean = ka["archive_compared"] > 0 and ka["archive_agree"] == ka["archive_compared"]
    post_clean = ka["post_fix_compared"] > 0 and ka["post_fix_agree"] == ka["post_fix_compared"]
    if dis_postfix:
        blocking.append(
            f"{len(dis_postfix)} outcome disagreement(s) on post-fix windows — the resolution "
            "pipeline is provably wrong somewhere; do not shadow-trade until root-caused."
        )
    elif dis and not (archive_clean and post_clean):
        investigate.append(
            f"{len(dis)} pre-fix disagreement(s) and the archive/post-fix views are not both "
            "clean — the failure is not fully explained by the fixed parser bug."
        )
    elif dis:
        conditions.append(
            f"{len(dis)} disagreement(s) exist but only on pre-fix windows, and the recorded "
            "archive implies the correct outcome for them — fully explained by the parser bug "
            "fixed in 3d645c5 and closed by 100% post-fix + archive agreement."
        )
    if post_clean:
        good.append(
            f"outcome agreement: post-fix {ka['post_fix_agree']}/{ka['post_fix_compared']} = "
            f"100%; archive-view {ka['archive_agree']}/{ka['archive_compared']} = "
            f"{100.0 * ka['archive_agree'] / ka['archive_compared']:.0f}%; overall bot-view "
            f"{ka['agreement_pct']:.1f}% ({ka['agree']}/{ka['windows_compared']})."
        )

    # -- K coverage and lag -----------------------------------------------------
    cov = ka["boundaries_captured"] / ka["boundaries_expected"] if ka["boundaries_expected"] else 0
    if cov < 0.98:
        blocking.append(f"K captured for only {cov:.1%} of boundaries.")
    else:
        good.append(
            f"K captured at {ka['boundaries_captured']}/{ka['boundaries_expected']} boundaries "
            "(100%)."
            if cov == 1
            else f"K coverage {cov:.1%}."
        )
    rlag = ka["recv_lag_ms_post_fix"]
    if rlag["p95"] is not None and rlag["p95"] > 2500:
        investigate.append(
            f"post-fix K receive lag p95 {rlag['p95']:.0f} ms > 2.5 s — the bot learns the "
            "price-to-beat too slowly."
        )
    else:
        good.append(
            f"post-fix K known to the bot median {rlag['median']:.0f} ms / p95 "
            f"{rlag['p95']:.0f} ms / max {rlag['max']:.0f} ms after the boundary."
        )

    # -- oracle cadence ----------------------------------------------------------
    og = oc["oracle_gap_ms"]
    td = oc["tail_density_last30s"]
    if og["median"] > 2000 or td["median"] < 15:
        investigate.append(
            f"oracle cadence thin (median gap {og['median']:.0f} ms, last-30s median "
            f"{td['median']:.0f} prints) — the late-window strategy may not have a signal."
        )
    else:
        good.append(
            f"oracle prints ~1/s (median gap {og['median']:.0f} ms); last-30s density median "
            f"{td['median']:.0f} prints, min {td['min']}."
        )
    post_sparse = [
        s for s in oc["sparse_windows_lt10"] if s["boundary_utc"] >= iso_utc(POST_FIX_FIRST_WINDOW)
    ]
    if post_sparse:
        investigate.append(
            f"{len(post_sparse)} post-fix window(s) with <10 prints in the last 30 s."
        )

    # RTDS delivery stalls: benign if rare, lossless, and covered by the
    # staleness guard; a real problem if frequent or if they delay K.
    stalls = oc.get("delivery_stalls_over_5s", [])
    if stalls:
        span_h = max((td["windows"] * 300) / 3600, 0.1)
        lost = sum(s["recovered_via_backfill_only"] for s in stalls)
        touched = sum(1 for s in stalls if s["window_tails_touched"])
        if len(stalls) / span_h > 6 or lost > 0:
            investigate.append(
                f"RTDS stalled >5s {len(stalls)} times ({len(stalls) / span_h:.1f}/h), "
                f"{lost} print(s) only recovered by backfill — delivery reliability is marginal."
            )
        else:
            conditions.append(
                f"RTDS pauses >5s ~{len(stalls) / span_h:.1f}×/h (max "
                f"{max(s['gap_s'] for s in stalls):.1f}s); prints arrive late but none are lost. "
                f"{touched} touched a window tail — expect the sniper to sit out occasionally "
                "on the staleness guard. Track this rate in shadow."
            )

    # -- feeds --------------------------------------------------------------------
    for feed, st in fh["feeds"].items():
        if not st.get("count"):
            blocking.append(f"feed {feed} recorded no frames.")
            continue
        n_over = st["gaps_over_threshold"]
        if feed == "rtds_chainlink":
            continue  # its gaps are the delivery stalls judged above
        if n_over > 5:
            investigate.append(f"{feed}: {n_over} gaps over {st['threshold_s']:.0f}s threshold.")
        else:
            good.append(
                f"{feed}: {st['count']:,} frames @ {st['fps']:.0f}/s, "
                f"{n_over} gap(s) over its {st['threshold_s']:.0f}s threshold."
            )

    # -- edge signal ----------------------------------------------------------------
    if bl["best_corr"] < 0.30:
        investigate.append(
            f"Binance→oracle max correlation only {bl['best_corr']:.2f} — lead signal weaker "
            "than the strategy premise assumes."
        )
    else:
        good.append(
            f"Binance leads the oracle: max corr {bl['best_corr']:.3f} at {bl['best_lag_ms']} ms "
            f"(zero-lag {bl['corr_at_0']:.2f})."
        )

    # -- sample size ------------------------------------------------------------------
    if ka["windows_compared"] < 30:
        investigate.append(
            f"only {ka['windows_compared']} resolved windows compared — too few to certify."
        )
    elif ka["windows_compared"] < 100:
        conditions.append(
            f"only {ka['windows_compared']} resolved windows (~5 h) vs the 24 h / ≥100-window "
            "target (FACTS.md 1.4) — shadow mode is itself the way to accumulate this; keep the "
            "recorder running and re-run this report before M2 calibration sign-off."
        )

    if blocking:
        return "NO-GO", blocking + investigate + conditions, good
    if investigate:
        return "INVESTIGATE", investigate + conditions, good
    return "GO", conditions, good


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("/opt/plv2/reports/M1_DATA_QUALITY.md"))
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    fh, fh_md = m1_feed_health.analyze(args.workdir)
    ka, ka_md = m1_k_accuracy.analyze(args.workdir, offline=args.offline)
    oc, oc_md = m1_oracle_cadence.analyze(args.workdir)
    bl, bl_md = m1_binance_lead.analyze(args.workdir)
    for name, obj in [
        ("feed_health", fh),
        ("k_accuracy", ka),
        ("oracle_cadence", oc),
        ("binance_lead", bl),
    ]:
        dump_json(obj, args.workdir / f"{name}.json")

    v, concerns, evidence = verdict(fh, ka, oc, bl)

    files = fh["per_file"]
    span_start = min(f["first_wall_ns"] for f in files if f["first_wall_ns"]) / 1e9
    span_end = max(f["last_wall_ns"] for f in files if f["last_wall_ns"]) / 1e9
    span_h = (span_end - span_start) / 3600

    L: list[str] = []
    L.append("# M1 Data-Quality Report — 5-minute BTC Up/Down recorder")
    L.append(
        f"\nGenerated {iso_utc_s(time.time())} · branch `claude/polymarket-btc-latency-bot-tz0a2q`"
    )
    shortfall = (
        " Note this is **less than the 24 h intended** for M1." if span_h < 24 else ""
    )
    L.append(
        f"\n**Recording analyzed: {iso_utc_s(span_start)} → {iso_utc_s(span_end)} "
        f"({span_h:.2f} h).**{shortfall} The recorder first came up at 05:57:48Z on 2026-07-02 "
        "and was restarted at 06:40:21Z to deploy the live-API fixes (commit `3d645c5`); "
        "everything below splits pre-fix vs post-fix where it matters."
    )
    L.append("\n**Raw archive inventory** (`/opt/plv2/data/raw/`):\n")
    L.append("| file | size (MB) | frames | first frame | last frame |")
    L.append("|---|---|---|---|---|")
    for f in files:
        L.append(
            f"| {f['file']} | {f['bytes'] / 1e6:,.0f} | {sum(f['frames'].values()):,} | "
            f"{iso_utc(f['first_wall_ns'] / 1e9)} | {iso_utc(f['last_wall_ns'] / 1e9)} |"
        )
    L.append("")
    incidents_file = args.out.parent / "M1_KNOWN_INCIDENTS.md"
    if incidents_file.exists():
        L.append("## 0. Known incidents & root causes\n")
        L.append(incidents_file.read_text().strip())
        L.append("")
    L.append(fh_md)
    L.append("")
    L.append(ka_md)
    L.append("")
    L.append(oc_md)
    L.append("")
    L.append(bl_md)
    L.append("")
    L.append("## 5. Verdict\n")
    L.append(f"# **{v}**\n")
    L.append("**Evidence for:**\n")
    for g in evidence:
        L.append(f"- ✅ {g}")
    if concerns:
        L.append("\n**Concerns / conditions:**\n")
        for c in concerns:
            L.append(f"- ⚠️ {c}")
    L.append(
        "\n**Reproduce:** `python scripts/m1_extract.py --workdir WD && "
        "python scripts/m1_report.py --workdir WD` (read-only against the live recorder; "
        "Gamma responses cached in WD)."
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(L) + "\n")
    print(f"wrote {args.out} — verdict: {v}", file=sys.stderr)


if __name__ == "__main__":
    main()
