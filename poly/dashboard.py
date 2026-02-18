"""Web dashboard for the Poly trading bot.

Single-file Flask app that reads poly_bot_state.json and poly_bot_log.jsonl
and serves a live dashboard. Auto-refreshes every 60 seconds.

No frontend build step. No JavaScript frameworks. Just server-rendered HTML
with inline CSS and a small Chart.js CDN include for the equity curve.

Usage:
  python -m poly.dashboard                    # http://localhost:8080
  python -m poly.dashboard --port 9090        # custom port
  python -m poly.dashboard --host 0.0.0.0     # expose to network

Requires: flask (pip install flask)
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import Flask, Response
except ImportError:
    Flask = None
    Response = None

STATE_FILE = Path("poly_bot_state.json")
LOG_FILE = Path("poly_bot_log.jsonl")

app = Flask(__name__) if Flask else None


def _load_state() -> dict:
    """Load current bot state."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _load_trades(max_trades: int = 2000) -> list[dict]:
    """Load trades from JSONL log (most recent first)."""
    if not LOG_FILE.exists():
        return []
    trades = []
    try:
        for line in LOG_FILE.read_text().strip().split("\n"):
            if line.strip():
                trades.append(json.loads(line))
    except Exception:
        pass
    return trades[-max_trades:]


def _compute_stats(trades: list[dict]) -> dict:
    """Compute summary statistics from trade list."""
    if not trades:
        return {
            "total": 0, "wins": 0, "losses": 0, "wr": 0,
            "pnl": 0, "avg_pnl": 0, "max_dd": 0, "stopouts": 0,
            "avg_edge": 0, "avg_bet": 0, "best_trade": 0, "worst_trade": 0,
            "streak_w": 0, "streak_l": 0, "today_trades": 0, "today_pnl": 0,
        }

    wins = sum(1 for t in trades if t.get("won"))
    losses = len(trades) - wins
    pnls = [t.get("pnl", 0) for t in trades]
    total_pnl = sum(pnls)
    stopouts = sum(1 for t in trades if t.get("stopped_out"))

    # Current streak
    streak_w, streak_l = 0, 0
    for t in reversed(trades):
        if t.get("won"):
            streak_w += 1
        else:
            break
    if streak_w == 0:
        for t in reversed(trades):
            if not t.get("won"):
                streak_l += 1
            else:
                break

    # Today's trades
    today_start = int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0
    ).timestamp())
    today = [t for t in trades if t.get("ts", 0) >= today_start]
    today_pnl = sum(t.get("pnl", 0) for t in today)

    return {
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "wr": wins / len(trades) if trades else 0,
        "pnl": total_pnl,
        "avg_pnl": total_pnl / len(trades) if trades else 0,
        "max_dd": max((t.get("drawdown", 0) for t in trades), default=0),
        "stopouts": stopouts,
        "avg_edge": sum(t.get("edge", 0) for t in trades) / len(trades) if trades else 0,
        "avg_bet": sum(t.get("bet", 0) for t in trades) / len(trades) if trades else 0,
        "best_trade": max(pnls) if pnls else 0,
        "worst_trade": min(pnls) if pnls else 0,
        "streak_w": streak_w,
        "streak_l": streak_l,
        "today_trades": len(today),
        "today_pnl": today_pnl,
    }


def _render_dashboard() -> str:
    """Render the full dashboard HTML."""
    state = _load_state()
    trades = _load_trades()
    stats = _compute_stats(trades)

    bankroll = state.get("bankroll", 0)
    starting = state.get("starting_bankroll", 0)
    peak = state.get("peak_bankroll", 0)
    roi = ((bankroll - starting) / starting * 100) if starting > 0 else 0
    mode = "DRY RUN" if state.get("dry_run", True) else "LIVE"
    order_type = "MAKER" if state.get("use_maker") else "TAKER"
    recent = state.get("recent_outcomes", [])

    # Equity curve data points
    bankroll_points = []
    for t in trades:
        ts = t.get("ts", 0)
        b = t.get("bankroll", 0)
        if ts and b:
            bankroll_points.append({"x": ts * 1000, "y": round(b, 2)})

    # Recent trades table rows
    trade_rows = ""
    for t in reversed(trades[-50:]):
        ts = t.get("ts", 0)
        time_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M") if ts else "?"
        won = t.get("won", False)
        stopped = t.get("stopped_out", False)
        pnl = t.get("pnl", 0)
        result_class = "win" if won else ("stop" if stopped else "loss")
        result_label = "WIN" if won else ("STOP" if stopped else "LOSS")
        fill_pct = t.get("fill_fraction", 1.0) * 100

        trade_rows += f"""<tr class="{result_class}">
            <td>{time_str}</td>
            <td>{t.get('side', '?')}</td>
            <td>{t.get('order_type', '?')}</td>
            <td>{t.get('model_p', 0):.1%}</td>
            <td>{t.get('edge', 0):+.1%}</td>
            <td>${t.get('bet', 0):.2f}</td>
            <td>{t.get('buy_price', 0):.3f}</td>
            <td>{fill_pct:.0f}%</td>
            <td class="pnl">${pnl:+.2f}</td>
            <td>${t.get('bankroll', 0):,.2f}</td>
            <td><span class="badge {result_class}">{result_label}</span></td>
        </tr>"""

    points_json = json.dumps(bankroll_points)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Poly Bot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid #21262d; padding-bottom: 16px; }}
  .header h1 {{ font-size: 1.4rem; color: #58a6ff; }}
  .header .mode {{ padding: 4px 12px; border-radius: 12px; font-size: 0.8rem; font-weight: bold; }}
  .mode.live {{ background: #1f6feb33; color: #58a6ff; border: 1px solid #1f6feb; }}
  .mode.dry {{ background: #f0883e33; color: #f0883e; border: 1px solid #f0883e; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; }}
  .card .label {{ font-size: 0.7rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card .value {{ font-size: 1.5rem; font-weight: bold; margin-top: 4px; }}
  .card .sub {{ font-size: 0.75rem; color: #8b949e; margin-top: 2px; }}
  .green {{ color: #3fb950; }}
  .red {{ color: #f85149; }}
  .yellow {{ color: #f0883e; }}
  .chart-container {{ background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; margin-bottom: 24px; }}
  .chart-container h2 {{ font-size: 0.9rem; color: #8b949e; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; }}
  th {{ text-align: left; padding: 8px; color: #8b949e; border-bottom: 1px solid #21262d; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px; }}
  td {{ padding: 6px 8px; border-bottom: 1px solid #21262d11; }}
  tr.win td {{ background: #3fb95008; }}
  tr.loss td {{ background: #f8514908; }}
  tr.stop td {{ background: #f0883e08; }}
  .pnl {{ font-weight: bold; }}
  tr.win .pnl {{ color: #3fb950; }}
  tr.loss .pnl, tr.stop .pnl {{ color: #f85149; }}
  .badge {{ padding: 2px 8px; border-radius: 10px; font-size: 0.65rem; font-weight: bold; }}
  .badge.win {{ background: #23863633; color: #3fb950; }}
  .badge.loss {{ background: #f8514933; color: #f85149; }}
  .badge.stop {{ background: #f0883e33; color: #f0883e; }}
  .outcomes {{ display: flex; gap: 6px; margin-bottom: 24px; }}
  .outcomes .pill {{ padding: 4px 12px; border-radius: 12px; font-size: 0.75rem; font-weight: bold; }}
  .outcomes .pill.up {{ background: #23863633; color: #3fb950; }}
  .outcomes .pill.down {{ background: #f8514933; color: #f85149; }}
  .table-container {{ background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; overflow-x: auto; }}
  .table-container h2 {{ font-size: 0.9rem; color: #8b949e; margin-bottom: 12px; }}
  .footer {{ text-align: center; color: #484f58; font-size: 0.7rem; margin-top: 24px; }}
</style>
</head>
<body>
<div class="header">
  <h1>Poly Bot</h1>
  <div>
    <span class="mode {'live' if mode == 'LIVE' else 'dry'}">{mode}</span>
    <span class="mode dry">{order_type}</span>
  </div>
</div>

<div class="cards">
  <div class="card">
    <div class="label">Bankroll</div>
    <div class="value {'green' if bankroll >= starting else 'red'}">${bankroll:,.2f}</div>
    <div class="sub">Started: ${starting:,.2f}</div>
  </div>
  <div class="card">
    <div class="label">P&L</div>
    <div class="value {'green' if stats['pnl'] >= 0 else 'red'}">${stats['pnl']:+,.2f}</div>
    <div class="sub">ROI: {roi:+.1f}%</div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value">{stats['wr']:.1%}</div>
    <div class="sub">{stats['wins']}W / {stats['losses']}L ({stats['total']} total)</div>
  </div>
  <div class="card">
    <div class="label">Today</div>
    <div class="value {'green' if stats['today_pnl'] >= 0 else 'red'}">${stats['today_pnl']:+,.2f}</div>
    <div class="sub">{stats['today_trades']} trades</div>
  </div>
  <div class="card">
    <div class="label">Max Drawdown</div>
    <div class="value red">{stats['max_dd']:.1%}</div>
    <div class="sub">Peak: ${peak:,.2f}</div>
  </div>
  <div class="card">
    <div class="label">Avg P&L / Trade</div>
    <div class="value {'green' if stats['avg_pnl'] >= 0 else 'red'}">${stats['avg_pnl']:+.2f}</div>
    <div class="sub">Avg bet: ${stats['avg_bet']:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Streak</div>
    <div class="value {'green' if stats['streak_w'] > 0 else 'red'}">{'W' + str(stats['streak_w']) if stats['streak_w'] > 0 else 'L' + str(stats['streak_l'])}</div>
    <div class="sub">Best: ${stats['best_trade']:+.2f} / Worst: ${stats['worst_trade']:+.2f}</div>
  </div>
  <div class="card">
    <div class="label">Stop-outs</div>
    <div class="value yellow">{stats['stopouts']}</div>
    <div class="sub">Avg edge: {stats['avg_edge']:+.1%}</div>
  </div>
</div>

<div class="outcomes">
  <span style="color: #8b949e; font-size: 0.75rem; line-height: 28px;">Recent:</span>
  {''.join(f'<span class="pill {"up" if o == "Up" else "down"}">{o}</span>' for o in recent)}
</div>

<div class="chart-container">
  <h2>Equity Curve</h2>
  <canvas id="equityChart" height="100"></canvas>
</div>

<div class="table-container">
  <h2>Recent Trades (last 50)</h2>
  <table>
    <thead>
      <tr>
        <th>Time</th><th>Side</th><th>Type</th><th>Model</th><th>Edge</th>
        <th>Bet</th><th>Price</th><th>Fill</th><th>P&L</th><th>Bank</th><th>Result</th>
      </tr>
    </thead>
    <tbody>{trade_rows}</tbody>
  </table>
</div>

<div class="footer">Updated: {now_str} &middot; Auto-refresh: 60s</div>

<script>
const points = {points_json};
if (points.length > 0) {{
  new Chart(document.getElementById('equityChart'), {{
    type: 'line',
    data: {{
      datasets: [{{
        data: points,
        borderColor: '#58a6ff',
        backgroundColor: '#58a6ff22',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{
          type: 'time',
          time: {{ unit: 'hour', displayFormats: {{ hour: 'MMM d HH:mm' }} }},
          grid: {{ color: '#21262d' }},
          ticks: {{ color: '#8b949e', font: {{ size: 10 }} }},
        }},
        y: {{
          grid: {{ color: '#21262d' }},
          ticks: {{ color: '#8b949e', font: {{ size: 10 }}, callback: v => '$' + v.toFixed(0) }},
        }}
      }}
    }}
  }});
}}
</script>
</body>
</html>"""


def _render_json_api() -> str:
    """Render JSON API response with current state and stats."""
    state = _load_state()
    trades = _load_trades()
    stats = _compute_stats(trades)
    return json.dumps({
        "state": state,
        "stats": stats,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "trades_count": len(trades),
    }, indent=2)


if app:
    @app.route("/")
    def index():
        return _render_dashboard()

    @app.route("/api/status")
    def api_status():
        return Response(_render_json_api(), mimetype="application/json")

    @app.route("/api/trades")
    def api_trades():
        trades = _load_trades()
        return Response(json.dumps(trades, indent=2), mimetype="application/json")


def run_dashboard(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run the dashboard web server."""
    if not Flask:
        print("Flask not installed. Run: pip install flask")
        print("Falling back to built-in HTTP server...")
        _run_builtin_server(host, port)
        return

    print(f"  Dashboard: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


def _run_builtin_server(host: str, port: int) -> None:
    """Fallback: serve dashboard using Python's built-in HTTP server."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "":
                html = _render_dashboard()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
            elif self.path == "/api/status":
                data = _render_json_api()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data.encode())
            else:
                self.send_error(404)

        def log_message(self, format, *args):
            pass  # suppress access logs

    server = HTTPServer((host, port), Handler)
    print(f"  Dashboard: http://{host}:{port} (built-in server)")
    server.serve_forever()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Poly Bot Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()
    run_dashboard(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
