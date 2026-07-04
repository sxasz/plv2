_Curated root-cause notes; maintained by hand, appended verbatim by `m1_report.py`. Dates UTC._

**INC-1 · 2026-07-03 00:15–02:15 · RTDS silent connection ("Too Many Requests") — 24 boundaries lost.**
The RTDS server drops each connection after a 2-hour lifetime. At the 00:15:01 drop, the reconnect's
subscription was answered with a plain `{"message": "Too Many Requests"}` control frame; the feed kept
the socket open (PING/PONG healthy) and received only TMR frames for the full 2-hour lifetime, until the
02:15:02 drop re-phased it onto a healthy connection. No oracle prints were received or archived for the
period, so 24 boundaries have no K, 25 windows could not be settled, and one filled shadow position was
stranded unsettled (annotated in the M2 report). **Fix required before M3:** treat any non-print control
message as fatal (backoff + reconnect), add a no-data watchdog (~30 s) independent of PING/PONG, and
settle stranded sessions from the Gamma resolution as a fallback.

**INC-2 · 2026-07-02 18:15 → 2026-07-03 04:15 · 2h reconnect cycle phase-locked onto window boundaries — 1 flipped outcome.**
The same 2-hour lifetime, when phased at :15:01, kills the connection milliseconds after a :15:00 window
boundary. The boundary print is then missed live and recovered only via the reconnect backfill dump, so
`k_captures` latches the *next* live print (+1 s/+2 s/+4 s observed at 20:15, 22:15, 02:15). At 20:15:00 the
resulting K error (+$2.24) flipped the implied outcome on a $0.03 close margin — the single post-fix
bot-view disagreement in §2 (the archive view remains 100%). The phase drifts with every irregular drop,
so any boundary can be hit. **Fix required before M3:** accept a backfill-recovered earlier boundary print
as a K correction within a grace period (with an incident log), and/or preemptively cycle the RTDS
connection mid-window so server-side drops never race a boundary.
