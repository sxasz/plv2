"""Read-only Flask dashboard (spec §9).

Reads ONLY from storage (SQLite + the engine's atomic status.json), never
from live engine state. Binds to localhost; reach it over an SSH tunnel.

Run:  python -m polymkt_bot.dashboard.app --config config.yaml
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string
from flask.wrappers import Response

from ..config import Config, load_config

PAGE = """<!doctype html>
<html><head><title>polymkt_bot</title>
<meta http-equiv="refresh" content="5">
<style>
 body{font-family:ui-monospace,monospace;background:#101418;color:#d8dee6;margin:2rem}
 table{border-collapse:collapse;margin:1rem 0;width:100%}
 td,th{border:1px solid #2a3138;padding:4px 10px;text-align:right;font-size:13px}
 th{background:#1a2027} td:first-child,th:first-child{text-align:left}
 h1,h2{font-weight:600} .bad{color:#ff6b6b} .ok{color:#69db7c}
 pre{background:#1a2027;padding:1rem;overflow-x:auto}
</style></head><body>
<h1>polymkt_bot</h1>
<h2>Live state</h2><pre>{{ status }}</pre>
<h2>Last {{ windows|length }} windows</h2>
<table><tr><th>window</th><th>slug</th><th>K</th><th>outcome</th><th>trades</th>
<th>stake</th><th>edge@entry</th><th>PnL</th><th>fees $</th></tr>
{% for w in windows %}<tr>
 <td>{{ w['window_start'] }}</td><td>{{ w['slug'] }}</td><td>{{ w['k'] }}</td>
 <td>{{ w['outcome'] or '—' }}</td><td>{{ w['n_trades'] }}</td>
 <td>{{ w['stake_usdc'] }}</td><td>{{ w['entry_edge'] }}</td>
 <td class="{{ 'ok' if (w['realized_pnl_usdc'] or 0) >= 0 else 'bad' }}">
     {{ w['realized_pnl_usdc'] }}</td>
 <td>{{ w['fees_usdc'] }}</td>
</tr>{% endfor %}</table>
<h2>Cumulative PnL net of fees: <span class="{{ 'ok' if total_pnl >= 0 else 'bad' }}">
{{ '%.4f'|format(total_pnl) }} USDC</span></h2>
<h2>Latency (µs, p50/p95 by hop)</h2>
<table><tr><th>hop</th><th>n</th><th>p50</th><th>p95</th></tr>
{% for row in latency %}<tr><td>{{ row[0] }}</td><td>{{ row[1] }}</td>
<td>{{ '%.0f'|format(row[2]) }}</td><td>{{ '%.0f'|format(row[3]) }}</td></tr>{% endfor %}
</table>
<h2>Recent incidents</h2>
<table><tr><th>wall_ns</th><th>feed</th><th>kind</th><th>detail</th></tr>
{% for i in incidents %}<tr><td>{{ i['wall_ns'] }}</td><td>{{ i['feed'] }}</td>
<td>{{ i['kind'] }}</td><td>{{ i['detail'] }}</td></tr>{% endfor %}</table>
</body></html>"""


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)
    db_path = Path(cfg.run.data_dir) / "bot.sqlite"
    status_path = Path(cfg.run.status_file)

    def q(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not db_path.exists():
            return []
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def read_status() -> dict[str, Any]:
        try:
            return json.loads(status_path.read_text())  # type: ignore[no-any-return]
        except (OSError, json.JSONDecodeError):
            return {"status": "engine not running or no status file"}

    @app.get("/")
    def index() -> str:
        windows = q("SELECT * FROM windows ORDER BY window_start DESC LIMIT 50")
        total = q("SELECT COALESCE(SUM(realized_pnl_usdc),0) t FROM windows")
        latency = [
            (
                row["hop"],
                row["n"],
                row["p50"],
                row["p95"],
            )
            for row in q(
                """SELECT hop, COUNT(*) n,
                   (SELECT micros FROM latency_samples l2 WHERE l2.hop=l1.hop
                    ORDER BY micros LIMIT 1 OFFSET CAST(COUNT(*)*0.5 AS INT)) p50,
                   (SELECT micros FROM latency_samples l2 WHERE l2.hop=l1.hop
                    ORDER BY micros LIMIT 1 OFFSET CAST(COUNT(*)*0.95 AS INT)) p95
                   FROM latency_samples l1 GROUP BY hop"""
            )
        ]
        incidents = q("SELECT * FROM incidents ORDER BY wall_ns DESC LIMIT 30")
        return render_template_string(
            PAGE,
            status=json.dumps(read_status(), indent=2),
            windows=windows,
            total_pnl=float(total[0]["t"]) if total else 0.0,
            latency=latency,
            incidents=incidents,
        )

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(read_status())

    @app.get("/api/windows")
    def api_windows() -> Response:
        return jsonify(q("SELECT * FROM windows ORDER BY window_start DESC LIMIT 500"))

    @app.get("/api/fills")
    def api_fills() -> Response:
        return jsonify(q("SELECT * FROM fills ORDER BY recv_mono_ns DESC LIMIT 500"))

    @app.get("/api/incidents")
    def api_incidents() -> Response:
        return jsonify(q("SELECT * FROM incidents ORDER BY wall_ns DESC LIMIT 500"))

    @app.get("/api/k_captures")
    def api_k() -> Response:
        return jsonify(q("SELECT * FROM k_captures ORDER BY window_start DESC LIMIT 500"))

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    app = create_app(cfg)
    # Localhost only (spec §9); use an SSH tunnel to reach it.
    app.run(host=cfg.dashboard.host, port=cfg.dashboard.port, debug=False)


if __name__ == "__main__":
    main()
