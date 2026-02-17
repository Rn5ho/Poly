#!/usr/bin/env python3
"""Poly — Polymarket BTC mispricing scanner.

Scans active Polymarket Bitcoin up/down markets, calculates fair
probabilities using a volatility-based model, and surfaces markets
where the crowd has mispriced the outcome.
"""

import argparse
import sys

from poly.btc import get_price, realized_volatility
from poly.model import evaluate_market
from poly.polymarket import fetch_btc_markets


def scan(edge_threshold: float = 0.03, vol_window: int = 168) -> None:
    """Run a full scan of BTC markets and print signals."""
    print("=" * 72)
    print("  POLY — Polymarket BTC Mispricing Scanner")
    print("=" * 72)

    # 1. Fetch BTC data
    print("\n[1/3] Fetching BTC price and volatility...")
    btc_price = get_price()
    vol = realized_volatility(vol_window)
    print(f"  BTC Price:  ${btc_price:,.2f}")
    print(f"  Volatility: {vol:.1%} annualized ({vol_window}h window)")

    # 2. Discover markets
    print("\n[2/3] Discovering Polymarket BTC markets...")
    markets = fetch_btc_markets()
    with_strike = [m for m in markets if m.strike is not None]
    print(f"  Found {len(markets)} markets ({len(with_strike)} with parseable strikes)")

    if not with_strike:
        print("\n  No markets with strike prices found. Exiting.")
        return

    # 3. Evaluate each market (skip expired / no-liquidity)
    print(f"\n[3/3] Evaluating markets (edge threshold: {edge_threshold:.0%})...")
    signals = []
    for m in with_strike:
        sig = evaluate_market(
            question=m.question,
            slug=m.slug,
            strike=m.strike,
            expiry=m.end_date,
            market_yes_price=m.mid if m.mid > 0 else m.yes_price,
            best_bid=m.best_bid,
            best_ask=m.best_ask,
            volume_24h=m.volume_24h,
            liquidity=m.liquidity,
            btc_price=btc_price,
            volatility=vol,
            edge_threshold=edge_threshold,
        )
        # Skip expired markets (0 hours left, no liquidity)
        if sig.hours_left <= 0 and sig.liquidity == 0:
            continue
        signals.append(sig)

    # Sort: actionable signals first, then by absolute edge descending
    signals.sort(key=lambda s: (s.side == "NO EDGE", -abs(s.edge)))

    # Print results
    actionable = [s for s in signals if s.side != "NO EDGE"]
    print(f"\n  {len(actionable)} actionable signals found\n")
    print("-" * 72)

    for sig in signals:
        _print_signal(sig)

    # Summary
    if actionable:
        print("=" * 72)
        print("  SUMMARY — Top Opportunities")
        print("=" * 72)
        for sig in actionable[:5]:
            direction = "↑ YES" if sig.side == "BUY YES" else "↓ NO"
            print(
                f"  {direction}  edge={sig.edge:+.1%}  "
                f"EV={sig.expected_value:+.1%}  "
                f"${sig.strike:,.0f}  "
                f"({sig.hours_left:.1f}h left)  "
                f"vol=${sig.volume_24h:,.0f}"
            )
        print()
    else:
        print("\n  No mispriced markets found. The crowd is efficient today.\n")


def _print_signal(sig) -> None:
    """Print a single signal."""
    if sig.side != "NO EDGE":
        marker = ">>> "
        label = f"*** {sig.side} ***"
    else:
        marker = "    "
        label = "no edge"

    print(f"{marker}{sig.question}")
    print(f"      Strike: ${sig.strike:,.0f}  |  BTC: ${sig.btc_price:,.2f}  |  Expiry: {sig.hours_left:.1f}h")
    print(f"      Model: {sig.model_prob:.1%}  |  Market: {sig.market_prob:.1%}  |  Edge: {sig.edge:+.1%}")
    print(f"      Bid/Ask: {sig.best_bid:.2f}/{sig.best_ask:.2f}  |  24h Vol: ${sig.volume_24h:,.0f}  |  Liq: ${sig.liquidity:,.0f}")
    print(f"      Signal: {label}  |  EV: {sig.expected_value:+.1%}")
    print(f"      https://polymarket.com/event/{sig.slug}")
    print("-" * 72)


def main():
    parser = argparse.ArgumentParser(
        description="Scan Polymarket BTC markets for mispriced binary outcomes."
    )
    parser.add_argument(
        "-e", "--edge",
        type=float,
        default=0.03,
        help="Minimum edge threshold to flag a signal (default: 0.03 = 3%%)",
    )
    parser.add_argument(
        "-w", "--vol-window",
        type=int,
        default=168,
        help="Hours of data for volatility calculation (default: 168 = 7 days)",
    )
    args = parser.parse_args()

    try:
        scan(edge_threshold=args.edge, vol_window=args.vol_window)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
