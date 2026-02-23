#!/usr/bin/env python3
"""Poly — Polymarket 5-minute BTC up/down trading system.

Modes:
  scan      One-shot scan of upcoming markets
  watch     Continuous scanning with live signals
  backtest  Validate model against resolved markets
  trade     Live trading (requires POLY_PRIVATE_KEY)
  autobot   Automated continuous trading bot
"""

import argparse
import sys
import time
from datetime import datetime, timezone

from poly.btc import get_snapshot
from poly.model import conditional_prob_up, evaluate_5m_market
from poly.polymarket import fetch_5m_markets, fetch_recent_outcomes


def scan(edge_threshold: float = 0.03, future_windows: int = 12) -> None:
    """Run a single scan of 5-minute BTC markets."""
    now = datetime.now(timezone.utc)
    print("=" * 72)
    print("  POLY — 5-Minute BTC Up/Down Scanner")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 72)

    print("\n[1/4] Fetching BTC snapshot...")
    snap = get_snapshot()
    price_src = "Chainlink" if snap.chainlink_price else "Exchange"
    print(f"  Price:        ${snap.price:,.2f} ({price_src})")
    print(f"  Momentum 5m:  {snap.momentum_5m:+.4%}")
    print(f"  Momentum 15m: {snap.momentum_15m:+.4%}")
    print(f"  Vol (1m):     {snap.volatility_1m:.1%} ann.")
    print(f"  Vol (1h):     {snap.volatility_1h:.1%} ann.")

    print("\n[2/4] Discovering 5-minute markets...")
    markets = fetch_5m_markets(past_windows=2, future_windows=future_windows)
    tradeable = [m for m in markets if m.is_tradeable]
    print(f"  Found {len(markets)} markets ({len(tradeable)} tradeable)")

    if not tradeable:
        print("\n  No tradeable markets found.")
        return

    # Fetch recent resolved outcomes for conditional model
    print("\n[3/4] Fetching recent outcomes for conditional model...")
    prev_outcomes = fetch_recent_outcomes(n=3)
    if prev_outcomes:
        outcomes_str = " -> ".join(prev_outcomes)
        print(f"  Recent: {outcomes_str}")
    else:
        print("  No recent outcomes available (using base rate)")

    model_up = conditional_prob_up(prev_outcomes)
    print(f"\n[4/4] Conditional P(Up) = {model_up:.1%}  |  Threshold: {edge_threshold:.0%}")
    print()

    signals = []
    for m in tradeable:
        sig = evaluate_5m_market(m, snap, edge_threshold, prev_outcomes=prev_outcomes)
        signals.append(sig)

    signals.sort(key=lambda s: (s.side == "NO EDGE", s.window_start))

    actionable = [s for s in signals if s.side != "NO EDGE"]
    print(f"  {len(actionable)} actionable / {len(signals)} total\n")
    print("-" * 72)

    for sig in signals:
        _print_signal(sig)

    if actionable:
        _print_summary(actionable)
    else:
        print("\n  Markets efficiently priced. No edge detected.\n")


def watch(edge_threshold: float = 0.03, interval: int = 60) -> None:
    """Continuously scan markets."""
    print(f"Watching markets every {interval}s. Ctrl+C to stop.\n")
    while True:
        try:
            scan(edge_threshold=edge_threshold)
            print(f"\n  Next scan in {interval}s...\n")
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return


def backtest(
    hours: int = 6,
    edge_threshold: float = 0.03,
    bankroll: float = 100.0,
    max_bet: float = 0.0,
    as_maker: bool = False,
    fill_rate: float = 1.0,
    use_real_prices: bool = False,
) -> None:
    """Run backtest against resolved markets."""
    from poly.backtest import print_backtest, run_backtest

    order_type = "MAKER ($0 fee)" if as_maker else "TAKER (1.56% fee)"
    extras = []
    if fill_rate < 1.0:
        extras.append(f"fill={fill_rate:.0%}")
    if use_real_prices:
        extras.append("real prices")
    extra_str = f"  |  {', '.join(extras)}" if extras else ""
    print("=" * 72)
    print(f"  BACKTEST — Last {hours} hours  |  ${bankroll:,.2f}  |  {order_type}{extra_str}")
    print("=" * 72)
    print()

    result = run_backtest(
        hours=hours,
        edge_threshold=edge_threshold,
        bankroll=bankroll,
        max_bet=max_bet,
        as_maker=as_maker,
        fill_rate=fill_rate,
        use_real_prices=use_real_prices,
    )
    print_backtest(result)


def trade(
    edge_threshold: float = 0.03,
    bankroll: float = 1000.0,
    max_bet: float = 50.0,
    dry_run: bool = True,
) -> None:
    """Live trading mode — scan and place orders on actionable signals.

    Only trades the NEAREST window (current or next).
    """
    from poly.execute import calculate_bet_size, place_market_order

    now = datetime.now(timezone.utc)
    print("=" * 72)
    mode_str = "DRY RUN" if dry_run else "LIVE"
    print(f"  TRADE MODE ({mode_str}) -- {now.strftime('%H:%M:%S UTC')}")
    print(f"  Bankroll: ${bankroll:,.2f}  |  Max bet: ${max_bet:,.2f}")
    print("=" * 72)

    snap = get_snapshot()
    price_src = "Chainlink" if snap.chainlink_price else "Exchange"
    print(f"\n  BTC: ${snap.price:,.2f} ({price_src})  |  Mom 5m: {snap.momentum_5m:+.4%}  |  Vol: {snap.volatility_1m:.1%}")

    markets = fetch_5m_markets(past_windows=0, future_windows=3)
    tradeable = [m for m in markets if m.is_tradeable]
    if not tradeable:
        print("  No tradeable markets. Waiting...")
        return

    # Fetch recent outcomes for conditional model
    prev_outcomes = fetch_recent_outcomes(n=3)
    if prev_outcomes:
        outcomes_str = " -> ".join(prev_outcomes)
        print(f"  Recent outcomes: {outcomes_str}")
        model_up = conditional_prob_up(prev_outcomes)
        print(f"  Conditional P(Up): {model_up:.1%}")

    # Evaluate all tradeable windows, prefer in-progress with edge
    signals = [evaluate_5m_market(m, snap, edge_threshold, prev_outcomes=prev_outcomes) for m in tradeable]
    actionable = [s for s in signals if s.side != "NO EDGE"]

    if actionable:
        # Prefer in-progress signals, then by absolute edge
        actionable.sort(key=lambda s: (-s.in_progress, -abs(s.edge)))
        sig = actionable[0]
    else:
        sig = signals[0]

    live_str = f" [{int(sig.seconds_remaining)}s left]" if sig.in_progress else ""
    print(f"\n  Window: {sig.question}{live_str}")
    print(f"  Model: {sig.model_prob_up:.1%} Up  |  Market: {sig.market_prob_up:.1%}  |  Edge: {sig.edge:+.1%}")

    if sig.side == "NO EDGE":
        print("  No edge. Skipping.\n")
        return

    bet_size = calculate_bet_size(bankroll, sig.kelly_fraction, max_bet)
    if bet_size <= 0:
        print(f"  Edge too small for minimum bet. Kelly={sig.kelly_fraction:.1%}. Skipping.\n")
        return

    print(f"\n  >>> {sig.side}  |  Kelly: {sig.kelly_fraction:.1%}  |  Bet: ${bet_size:.2f}  |  EV: {sig.expected_value:+.1%}")

    if dry_run:
        print(f"  [DRY RUN] Would place ${bet_size:.2f} on {sig.side}")
        print()
        return

    nearest = tradeable[0]
    result = place_market_order(sig, nearest, bet_size)
    if result.success:
        print(f"  ORDER PLACED: {result.order_id}")
    else:
        print(f"  ORDER FAILED: {result.error}")
    print()


def _print_signal(sig) -> None:
    """Print a single signal."""
    t = datetime.fromtimestamp(sig.window_start, tz=timezone.utc)
    end_min = (t.minute + 5) % 60
    end_hour = t.hour + (1 if t.minute + 5 >= 60 else 0)
    window_str = f"{t.strftime('%H:%M')}-{end_hour:02d}:{end_min:02d} UTC"

    if sig.side != "NO EDGE":
        marker = ">>> "
    else:
        marker = "    "

    if sig.in_progress:
        remaining = int(sig.seconds_remaining)
        status = f" [LIVE {remaining}s left]"
    elif sig.minutes_until <= 0:
        status = " [LIVE]"
    else:
        status = f" [in {sig.minutes_until:.0f}m]"

    print(f"{marker}{window_str}{status}  {sig.question}")
    mkt_label = f"Market: {sig.market_prob_up:.0%} Up/{1-sig.market_prob_up:.0%} Dn"
    print(f"      Model: {sig.model_prob_up:.1%} Up  |  {mkt_label}  |  Edge: {sig.edge:+.1%}")
    print(f"      Mom: {sig.momentum_5m:+.3%}  |  Vol: {sig.vol_1m:.1%}  |  Spread: {sig.spread:.2f}  |  Liq: ${sig.liquidity:,.0f}")
    if sig.side != "NO EDGE":
        prev_str = f"  |  Prev: {sig.prev_outcome}" if sig.prev_outcome else ""
        print(f"      Kelly: {sig.kelly_fraction:.1%}  |  EV: {sig.expected_value:+.1%}{prev_str}")
    print("-" * 72)


def _print_summary(actionable: list) -> None:
    """Print summary of top opportunities."""
    print("=" * 72)
    print("  TOP OPPORTUNITIES")
    print("=" * 72)
    for sig in actionable[:5]:
        arrow = "^UP " if sig.side == "BUY UP" else "vDOW"
        t = datetime.fromtimestamp(sig.window_start, tz=timezone.utc)
        end_min = (t.minute + 5) % 60
        print(
            f"  {arrow}  edge={sig.edge:+.1%}  "
            f"EV={sig.expected_value:+.1%}  "
            f"Kelly={sig.kelly_fraction:.0%}  "
            f"in {sig.minutes_until:.0f}m  "
            f"{t.strftime('%H:%M')}-{end_min:02d} UTC  "
            f"liq=${sig.liquidity:,.0f}"
        )
    print()


def _show_status() -> None:
    """Print current bot status from saved state and trade log."""
    import json
    from pathlib import Path

    state_file = Path("poly_bot_state.json")
    log_file = Path("poly_bot_log.jsonl")

    print("=" * 72)
    print("  POLY BOT STATUS")
    print("=" * 72)

    # Load state
    if state_file.exists():
        state = json.loads(state_file.read_text())
        bankroll = state.get("bankroll", 0)
        starting = state.get("starting_bankroll", 0)
        peak = state.get("peak_bankroll", 0)
        trades = state.get("total_trades", 0)
        wins = state.get("total_wins", 0)
        pnl = state.get("total_pnl", 0)
        max_dd = state.get("max_drawdown", 0)
        stopouts = state.get("total_stopouts", 0)
        recent = state.get("recent_outcomes", [])
        mode = "DRY RUN" if state.get("dry_run", True) else "LIVE"
        order_type = "MAKER" if state.get("use_maker") else "TAKER"
        wr = wins / trades if trades > 0 else 0
        roi = ((bankroll - starting) / starting) if starting > 0 else 0

        print(f"\n  Mode:        {mode} ({order_type})")
        print(f"  Started:     {state.get('started_at', '?')}")
        print(f"\n  Bankroll:    ${bankroll:,.2f}")
        print(f"  Starting:    ${starting:,.2f}")
        print(f"  Peak:        ${peak:,.2f}")
        print(f"  P&L:         ${pnl:+,.2f}  ({roi:+.1%} ROI)")
        print(f"\n  Trades:      {trades}")
        print(f"  Win rate:    {wr:.1%}  ({wins}W / {trades - wins}L)")
        print(f"  Max DD:      {max_dd:.1%}")
        print(f"  Stop-outs:   {stopouts}")
        print(f"\n  Recent:      {' -> '.join(recent) if recent else 'N/A'}")
    else:
        print("\n  No saved state found (poly_bot_state.json)")

    # Load recent trades from log
    if log_file.exists():
        log_lines = log_file.read_text().strip().split("\n")
        log_trades = []
        for line in log_lines:
            if line.strip():
                try:
                    log_trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        if log_trades:
            print(f"\n  --- LAST 10 TRADES ---")
            print(f"  {'Time UTC':>14}  {'Side':>8}  {'Type':>6}  {'Bet':>8}  {'P&L':>9}  {'Bank':>9}  {'Result':>6}")
            print(f"  {'-'*14}  {'-'*8}  {'-'*6}  {'-'*8}  {'-'*9}  {'-'*9}  {'-'*6}")
            for t in log_trades[-10:]:
                ts = t.get("ts", 0)
                dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
                time_str = dt.strftime("%m-%d %H:%M") if dt else "?"
                result = "WIN" if t.get("won") else ("STOP" if t.get("stopped_out") else "LOSS")
                pnl_val = t.get("pnl", 0)
                print(
                    f"  {time_str:>14}  {t.get('side', '?'):>8}  "
                    f"{t.get('order_type', '?'):>6}  "
                    f"${t.get('bet', 0):>7.2f}  "
                    f"{'+'if pnl_val>=0 else ''}${pnl_val:>7.2f}  "
                    f"${t.get('bankroll', 0):>8.2f}  "
                    f"{result:>6}"
                )

            # Today's stats
            today_start = int(datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0
            ).timestamp())
            today = [t for t in log_trades if t.get("ts", 0) >= today_start]
            if today:
                today_wins = sum(1 for t in today if t.get("won"))
                today_pnl = sum(t.get("pnl", 0) for t in today)
                today_wr = today_wins / len(today) if today else 0
                print(f"\n  --- TODAY ---")
                print(f"  Trades: {len(today)}  |  WR: {today_wr:.1%}  |  P&L: ${today_pnl:+,.2f}")
    else:
        print("\n  No trade log found (poly_bot_log.jsonl)")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket 5-minute BTC up/down trading system.",
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # scan
    p_scan = sub.add_parser("scan", help="One-shot scan of upcoming markets")
    p_scan.add_argument("-e", "--edge", type=float, default=0.03)
    p_scan.add_argument("-n", "--windows", type=int, default=12)

    # watch
    p_watch = sub.add_parser("watch", help="Continuous scanning")
    p_watch.add_argument("-e", "--edge", type=float, default=0.03)
    p_watch.add_argument("-i", "--interval", type=int, default=60)

    # backtest
    p_bt = sub.add_parser("backtest", help="Validate model against history")
    p_bt.add_argument("-H", "--hours", type=int, default=6)
    p_bt.add_argument("-e", "--edge", type=float, default=0.03)
    p_bt.add_argument("-b", "--bankroll", type=float, default=100.0, help="Starting bankroll (default $100)")
    p_bt.add_argument("-m", "--max-bet", type=float, default=0.0, help="Max bet per trade (default: 10%% of bankroll)")
    p_bt.add_argument("--maker", action="store_true", help="Simulate as maker ($0 fee, buy at bid)")
    p_bt.add_argument("--fill-rate", type=float, default=1.0, help="Simulated maker fill rate 0.0-1.0 (default 1.0)")
    p_bt.add_argument("--real-prices", action="store_true", help="Use real historical market prices from CLOB API")

    # trade
    p_trade = sub.add_parser("trade", help="Live trading (or dry run)")
    p_trade.add_argument("-e", "--edge", type=float, default=0.03)
    p_trade.add_argument("-b", "--bankroll", type=float, default=1000.0)
    p_trade.add_argument("-m", "--max-bet", type=float, default=50.0)
    p_trade.add_argument("--live", action="store_true", help="Actually place orders (default: dry run)")

    # autobot
    p_auto = sub.add_parser("autobot", help="Automated continuous trading bot")
    p_auto.add_argument("-b", "--bankroll", type=float, default=500.0, help="Starting bankroll (default $500)")
    p_auto.add_argument("-e", "--edge", type=float, default=0.03, help="Edge threshold (default 0.03)")
    p_auto.add_argument("-k", "--kelly", type=float, default=0.25, help="Kelly multiplier (default 0.25 = quarter)")
    p_auto.add_argument("--max-bet-pct", type=float, default=0.10, help="Max bet as %% of bankroll")
    p_auto.add_argument("--live", action="store_true", help="Place real orders (default: dry run)")
    p_auto.add_argument("--fresh", action="store_true", help="Start fresh (ignore saved state)")
    p_auto.add_argument("--maker", action="store_true", help="Use maker orders ($0 fee, recommended)")
    p_auto.add_argument("--taker", action="store_true", help="Use taker orders (1.56% fee)")
    p_auto.add_argument("--stoploss", action="store_true", help="Enable stop-loss during live windows")
    p_auto.add_argument("--dip", action="store_true", help="Dip-buy mode: buy Up when it crashes mid-window")
    p_auto.add_argument("--arb", action="store_true", help="Arb mode: buy both sides when combined < $1")

    # status
    sub.add_parser("status", help="Show current bot status from saved state/logs")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Run web dashboard")
    p_dash.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p_dash.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")

    args = parser.parse_args()

    try:
        if args.command == "scan":
            scan(edge_threshold=args.edge, future_windows=args.windows)
        elif args.command == "watch":
            watch(edge_threshold=args.edge, interval=args.interval)
        elif args.command == "backtest":
            backtest(
                hours=args.hours,
                edge_threshold=args.edge,
                bankroll=args.bankroll,
                max_bet=args.max_bet,
                as_maker=getattr(args, 'maker', False),
                fill_rate=getattr(args, 'fill_rate', 1.0),
                use_real_prices=getattr(args, 'real_prices', False),
            )
        elif args.command == "trade":
            trade(
                edge_threshold=args.edge,
                bankroll=args.bankroll,
                max_bet=args.max_bet,
                dry_run=not args.live,
            )
        elif args.command == "autobot":
            from poly.autobot import run_bot

            use_maker = args.maker or not args.taker
            bot_mode = "default"
            if args.dip:
                bot_mode = "dip"
            elif args.arb:
                bot_mode = "arb"
            run_bot(
                bankroll=args.bankroll,
                edge_threshold=args.edge,
                max_bet_pct=args.max_bet_pct,
                kelly_mult=args.kelly,
                dry_run=not args.live,
                resume=not args.fresh,
                use_maker=use_maker,
                enable_stoploss=args.stoploss,
                mode=bot_mode,
            )
        elif args.command == "status":
            _show_status()
        elif args.command == "dashboard":
            from poly.dashboard import run_dashboard
            run_dashboard(host=args.host, port=args.port)
        else:
            # Default: scan
            scan()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
