#!/usr/bin/env python3
"""Poly — Polymarket 5-minute BTC up/down scanner.

Scans upcoming 5-minute Bitcoin markets on Polymarket, calculates
fair probabilities using momentum and volatility signals, and
surfaces markets where the crowd has mispriced the outcome.
"""

import argparse
import sys
import time
from datetime import datetime, timezone

from poly.btc import get_snapshot
from poly.model import evaluate_5m_market
from poly.polymarket import fetch_5m_markets


def scan(edge_threshold: float = 0.03, future_windows: int = 12) -> None:
    """Run a single scan of 5-minute BTC markets."""
    now = datetime.now(timezone.utc)
    print("=" * 72)
    print("  POLY — 5-Minute BTC Up/Down Scanner")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 72)

    # 1. BTC snapshot
    print("\n[1/3] Fetching BTC snapshot...")
    snap = get_snapshot()
    print(f"  Price:        ${snap.price:,.2f}")
    print(f"  Momentum 5m:  {snap.momentum_5m:+.4%}")
    print(f"  Momentum 15m: {snap.momentum_15m:+.4%}")
    print(f"  Vol (1m):     {snap.volatility_1m:.1%} ann.")
    print(f"  Vol (1h):     {snap.volatility_1h:.1%} ann.")

    # 2. Discover markets
    print("\n[2/3] Discovering 5-minute markets...")
    markets = fetch_5m_markets(past_windows=2, future_windows=future_windows)
    tradeable = [m for m in markets if m.is_tradeable]
    print(f"  Found {len(markets)} markets ({len(tradeable)} tradeable)")

    if not tradeable:
        print("\n  No tradeable markets found.")
        return

    # 3. Evaluate
    from poly.model import fair_prob_up
    model_now = fair_prob_up(snap, minutes_ahead=0)
    print(f"\n[3/3] Model P(Up) next window = {model_now:.1%}  (edge threshold: {edge_threshold:.0%})")
    mom_dir = "UP" if snap.momentum_5m > 0 else "DOWN" if snap.momentum_5m < 0 else "FLAT"
    print(f"  Momentum direction: {mom_dir}")
    print()

    signals = []
    for m in tradeable:
        sig = evaluate_5m_market(m, snap, edge_threshold)
        signals.append(sig)

    # Sort: actionable first, then by time
    signals.sort(key=lambda s: (s.side == "NO EDGE", s.window_start))

    actionable = [s for s in signals if s.side != "NO EDGE"]
    print(f"  {len(actionable)} actionable / {len(signals)} total\n")
    print("-" * 72)

    for sig in signals:
        _print_signal(sig)

    if actionable:
        print("=" * 72)
        print("  TOP OPPORTUNITIES")
        print("=" * 72)
        for sig in actionable[:5]:
            arrow = "^UP " if sig.side == "BUY UP" else "vDOW"
            t = datetime.fromtimestamp(sig.window_start, tz=timezone.utc)
            print(
                f"  {arrow}  edge={sig.edge:+.1%}  "
                f"EV={sig.expected_value:+.1%}  "
                f"in {sig.minutes_until:.0f}m  "
                f"{t.strftime('%H:%M')}-{(t.minute+5)%60:02d} UTC  "
                f"liq=${sig.liquidity:,.0f}"
            )
        print()
    else:
        print("\n  Markets efficiently priced. No edge detected.\n")


def watch(edge_threshold: float = 0.03, interval: int = 60) -> None:
    """Continuously scan markets at the given interval (seconds)."""
    print(f"Watching markets every {interval}s. Press Ctrl+C to stop.\n")
    while True:
        try:
            scan(edge_threshold=edge_threshold)
            print(f"\n  Next scan in {interval}s...\n")
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return


def _print_signal(sig) -> None:
    """Print a single signal."""
    t = datetime.fromtimestamp(sig.window_start, tz=timezone.utc)
    window_str = f"{t.strftime('%H:%M')}-{t.strftime('%H')}:{(t.minute+5)%60:02d} UTC"

    if sig.side != "NO EDGE":
        marker = ">>> "
        label = f"*** {sig.side} ***"
    else:
        marker = "    "
        label = "no edge"

    status = ""
    if sig.minutes_until <= 0:
        status = " [LIVE]"
    elif sig.minutes_until <= 5:
        status = f" [in {sig.minutes_until:.0f}m]"
    else:
        status = f" [in {sig.minutes_until:.0f}m]"

    print(f"{marker}{window_str}{status}  {sig.question}")
    print(f"      Model: {sig.model_prob_up:.1%} Up  |  Market: {sig.market_prob_up:.1%} Up  |  Edge: {sig.edge:+.1%}")
    print(f"      Mom 5m: {sig.momentum_5m:+.3%}  |  Vol: {sig.vol_1m:.1%} ann.  |  Spread: {sig.spread:.2f}")
    print(f"      Bid/Ask: {sig.best_bid:.2f}/{sig.best_ask:.2f}  |  Liq: ${sig.liquidity:,.0f}")
    print(f"      Signal: {label}  |  EV: {sig.expected_value:+.1%}")
    print("-" * 72)


def main():
    parser = argparse.ArgumentParser(
        description="Scan Polymarket 5-minute BTC up/down markets for mispricing."
    )
    parser.add_argument(
        "-e", "--edge",
        type=float,
        default=0.03,
        help="Minimum edge to flag a signal (default: 0.03 = 3%%)",
    )
    parser.add_argument(
        "-n", "--windows",
        type=int,
        default=12,
        help="Number of future 5-min windows to scan (default: 12 = 1 hour)",
    )
    parser.add_argument(
        "-w", "--watch",
        action="store_true",
        help="Continuously scan (re-scan every 60s)",
    )
    parser.add_argument(
        "-i", "--interval",
        type=int,
        default=60,
        help="Scan interval in seconds for watch mode (default: 60)",
    )
    args = parser.parse_args()

    try:
        if args.watch:
            watch(edge_threshold=args.edge, interval=args.interval)
        else:
            scan(edge_threshold=args.edge, future_windows=args.windows)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
