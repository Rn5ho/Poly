#!/usr/bin/env python3
"""Poly — Polymarket 5-minute BTC up/down trading system.

Modes:
  scan      One-shot scan of upcoming markets
  watch     Continuous scanning with live signals
  backtest  Validate model against resolved markets
  trade     Live trading (requires POLY_PRIVATE_KEY)
"""

import argparse
import sys
import time
from datetime import datetime, timezone

from poly.btc import get_snapshot
from poly.model import evaluate_5m_market, fair_prob_up
from poly.polymarket import fetch_5m_markets


def scan(edge_threshold: float = 0.03, future_windows: int = 12) -> None:
    """Run a single scan of 5-minute BTC markets."""
    now = datetime.now(timezone.utc)
    print("=" * 72)
    print("  POLY — 5-Minute BTC Up/Down Scanner")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 72)

    print("\n[1/3] Fetching BTC snapshot...")
    snap = get_snapshot()
    price_src = "Chainlink" if snap.chainlink_price else "Exchange"
    print(f"  Price:        ${snap.price:,.2f} ({price_src})")
    print(f"  Momentum 5m:  {snap.momentum_5m:+.4%}")
    print(f"  Momentum 15m: {snap.momentum_15m:+.4%}")
    print(f"  Vol (1m):     {snap.volatility_1m:.1%} ann.")
    print(f"  Vol (1h):     {snap.volatility_1h:.1%} ann.")

    print("\n[2/3] Discovering 5-minute markets...")
    markets = fetch_5m_markets(past_windows=2, future_windows=future_windows)
    tradeable = [m for m in markets if m.is_tradeable]
    print(f"  Found {len(markets)} markets ({len(tradeable)} tradeable)")

    if not tradeable:
        print("\n  No tradeable markets found.")
        return

    model_now = fair_prob_up(snap, minutes_ahead=0)
    mom_dir = "UP" if snap.momentum_5m > 0 else "DOWN" if snap.momentum_5m < 0 else "FLAT"
    print(f"\n[3/3] Model P(Up) next window = {model_now:.1%}  |  Momentum: {mom_dir}  |  Threshold: {edge_threshold:.0%}")
    print()

    signals = []
    for m in tradeable:
        sig = evaluate_5m_market(m, snap, edge_threshold)
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
) -> None:
    """Run backtest against resolved markets."""
    from poly.backtest import print_backtest, run_backtest

    print("=" * 72)
    print(f"  BACKTEST — Last {hours} hours  |  Starting bankroll: ${bankroll:,.2f}")
    print("=" * 72)
    print()

    result = run_backtest(
        hours=hours,
        edge_threshold=edge_threshold,
        bankroll=bankroll,
        max_bet=max_bet,
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
    print(f"  TRADE MODE ({mode_str}) — {now.strftime('%H:%M:%S UTC')}")
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

    # Evaluate all tradeable windows, prefer in-progress with edge
    signals = [evaluate_5m_market(m, snap, edge_threshold) for m in tradeable]
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
        print(f"      Kelly: {sig.kelly_fraction:.1%}  |  EV: {sig.expected_value:+.1%}")
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

    # trade
    p_trade = sub.add_parser("trade", help="Live trading (or dry run)")
    p_trade.add_argument("-e", "--edge", type=float, default=0.03)
    p_trade.add_argument("-b", "--bankroll", type=float, default=1000.0)
    p_trade.add_argument("-m", "--max-bet", type=float, default=50.0)
    p_trade.add_argument("--live", action="store_true", help="Actually place orders (default: dry run)")

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
            )
        elif args.command == "trade":
            trade(
                edge_threshold=args.edge,
                bankroll=args.bankroll,
                max_bet=args.max_bet,
                dry_run=not args.live,
            )
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
