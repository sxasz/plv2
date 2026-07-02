#!/usr/bin/env bash
# Disk-space guard + compaction for the M1 recorder (cron, runs as root).
#
# Two jobs, in order:
#   1. gzip raw .jsonl files the recorder is done writing (rotated hourly —
#      see polymkt_bot/storage/recorder.py — so anything not modified in the
#      last 2 hours is guaranteed complete). Never deletes data: gzip is
#      lossless and reversible, only the on-disk representation shrinks.
#   2. Re-check free space; if it's still below the threshold after
#      compaction, stop the recorder rather than let it run the disk to 0
#      and corrupt an in-flight write.
#
# Safe to re-run any time (cron does so every 15 min): already-.gz files are
# skipped, and stopping an already-stopped service is a no-op.

set -euo pipefail

DATA_RAW="/opt/plv2/data/raw"
SERVICE="polymkt-recorder"
MIN_FREE_PCT=5
COMPRESS_AGE_MIN=120

log() {
    logger -t polymkt-guard -p daemon.warning -- "$*"
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

free_pct() {
    df --output=pcent "$DATA_RAW" | tail -1 | tr -dc '0-9'
}

# -- 1. compress rotated-away raw files ---------------------------------------
compressed_any=0
while IFS= read -r -d '' f; do
    if gzip -f "$f"; then
        compressed_any=1
        log "compressed $(basename "$f")"
    else
        log "WARNING: gzip failed on $f"
    fi
done < <(find "$DATA_RAW" -maxdepth 1 -name '*.jsonl' -mmin "+${COMPRESS_AGE_MIN}" -print0)

# -- 2. disk-space safety check -----------------------------------------------
used_pct=$(free_pct)
free=$((100 - used_pct))

if [ "$free" -lt "$MIN_FREE_PCT" ]; then
    if systemctl is-active --quiet "$SERVICE"; then
        log "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        log "!! DISK SPACE CRITICAL: ${free}% free (< ${MIN_FREE_PCT}%)   !!"
        log "!! Stopping ${SERVICE} to avoid corrupting an in-flight write !!"
        log "!! Free up space (or grow the disk), then:                   !!"
        log "!!   sudo systemctl start ${SERVICE}                          !!"
        log "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        systemctl stop "$SERVICE"
    else
        log "WARNING: disk free ${free}% (< ${MIN_FREE_PCT}%) and ${SERVICE} already stopped"
    fi
fi
