"""Automated trading bot for 5-minute BTC up/down markets.

Runs continuously, placing trades every 5-minute window based on
the conditional mean-reversion model. Tracks its own bankroll,
trade history, and performance metrics.

Usage:
  python -m poly.autobot                       # dry run, $500 bankroll
  python -m poly.autobot --bankroll 1000       # dry run, $1000
  python -m poly.autobot --live                # REAL trading
  python -m poly.autobot --live --bankroll 500 # REAL, $500

The bot:
1. Waits for each 5-minute window boundary
2. Fetches the most recent resolved outcomes
3. Computes conditional P(Up) using graduated Down-streak model
4. If edge > threshold, sizes a quarter-Kelly bet and places it
5. Waits for resolution, updates bankroll, repeats
"""

import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from poly.model import (
    DEFAULT_BUY_PRICE,
    DEFAULT_EDGE_THRESHOLD,
    KELLY_MULTIPLIER,
    MAX_BET_PCT,
    MIN_BET,
    conditional_prob_up,
    kelly_fraction,
    net_odds_after_fees,
)
from poly.polymarket import (
    WINDOW_SECONDS,
    _fetch_5m_market,
    _fetch_resolved_outcome,
    fetch_recent_outcomes,
)

# How early (seconds) before window start to place orders
ORDER_LEAD_TIME = 30
# How long after window end to wait for resolution
RESOLUTION_WAIT = 60
# How often to poll for resolution
RESOLUTION_POLL = 10
# Max time to wait for resolution before giving up
RESOLUTION_TIMEOUT = 300

STATE_FILE = Path("poly_bot_state.json")
LOG_FILE = Path("poly_bot_log.jsonl")


@dataclass
class BotState:
    """Persistent bot state, saved to disk between iterations."""

    bankroll: float = 500.0
    starting_bankroll: float = 500.0
    peak_bankroll: float = 500.0
    total_trades: int = 0
    total_wins: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    recent_outcomes: list[str] = field(default_factory=list)
    last_window_ts: int = 0
    started_at: str = ""
    dry_run: bool = True

    @property
    def win_rate(self) -> float:
        return self.total_wins / self.total_trades if self.total_trades else 0.0

    @property
    def current_drawdown(self) -> float:
        if self.peak_bankroll <= 0:
            return 0.0
        return (self.peak_bankroll - self.bankroll) / self.peak_bankroll

    def save(self) -> None:
        STATE_FILE.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls) -> "BotState":
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            return cls(**data)
        return cls()


def _log_trade(entry: dict) -> None:
    """Append a trade entry to the JSONL log."""
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _next_window_ts() -> int:
    """Get the start timestamp of the next 5-minute window."""
    now = int(time.time())
    current = (now // WINDOW_SECONDS) * WINDOW_SECONDS
    if now == current:
        return current + WINDOW_SECONDS
    return current + WINDOW_SECONDS


def _current_window_ts() -> int:
    """Get the start timestamp of the current 5-minute window."""
    now = int(time.time())
    return (now // WINDOW_SECONDS) * WINDOW_SECONDS


def _wait_for_resolution(window_ts: int) -> str | None:
    """Wait for a window to resolve and return the outcome."""
    window_end = window_ts + WINDOW_SECONDS
    # Wait until window ends + buffer
    now = time.time()
    if now < window_end + RESOLUTION_WAIT:
        wait = window_end + RESOLUTION_WAIT - now
        _print(f"    Waiting {wait:.0f}s for resolution...")
        time.sleep(wait)

    # Poll for resolution
    deadline = time.time() + RESOLUTION_TIMEOUT
    while time.time() < deadline:
        outcome = _fetch_resolved_outcome(window_ts)
        if outcome:
            return outcome
        time.sleep(RESOLUTION_POLL)

    return None


def _print(msg: str) -> None:
    """Print with timestamp."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")


def run_bot(
    bankroll: float = 500.0,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    max_bet_pct: float = MAX_BET_PCT,
    kelly_mult: float = KELLY_MULTIPLIER,
    dry_run: bool = True,
    resume: bool = True,
) -> None:
    """Run the automated trading bot.

    Args:
        bankroll: Starting bankroll in USD.
        edge_threshold: Minimum edge to trade (default 3%).
        max_bet_pct: Max bet as fraction of bankroll (default 10%).
        kelly_mult: Kelly multiplier (default 0.25 = quarter-Kelly).
        dry_run: If True, simulate trades without placing orders.
        resume: If True, resume from saved state.
    """
    # Initialize or resume state
    if resume and STATE_FILE.exists():
        state = BotState.load()
        _print(f"Resumed from saved state: ${state.bankroll:.2f} bankroll, {state.total_trades} trades")
    else:
        state = BotState(
            bankroll=bankroll,
            starting_bankroll=bankroll,
            peak_bankroll=bankroll,
            started_at=_now_utc(),
            dry_run=dry_run,
        )

    state.dry_run = dry_run
    mode = "DRY RUN" if dry_run else "LIVE"

    # Lazy import for live trading
    place_order = None
    if not dry_run:
        from poly.execute import place_market_order
        place_order = place_market_order

    # Handle graceful shutdown
    shutdown = False

    def _shutdown(sig, frame):
        nonlocal shutdown
        shutdown = True
        _print("Shutting down gracefully...")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Print banner
    print()
    print("=" * 72)
    print(f"  POLY AUTOBOT ({mode})")
    print(f"  Bankroll: ${state.bankroll:,.2f}  |  Kelly: {kelly_mult:.0%}  |  Edge: {edge_threshold:.0%}")
    print(f"  Started: {state.started_at}")
    if state.total_trades > 0:
        print(f"  Trades: {state.total_trades}  |  WR: {state.win_rate:.1%}  |  P&L: ${state.total_pnl:+,.2f}")
    print("=" * 72)
    print()

    # Bootstrap recent outcomes if we don't have them
    if not state.recent_outcomes:
        _print("Fetching recent outcomes for conditional model...")
        state.recent_outcomes = fetch_recent_outcomes(n=5)
        if state.recent_outcomes:
            _print(f"  Recent: {' → '.join(state.recent_outcomes)}")
        else:
            _print("  No recent outcomes (will use base rate)")

    # Main loop
    while not shutdown:
        try:
            _run_one_cycle(state, edge_threshold, max_bet_pct, kelly_mult, place_order)
            state.save()
        except KeyboardInterrupt:
            break
        except Exception as e:
            _print(f"ERROR: {e}")
            time.sleep(30)  # back off on errors

    # Final save
    state.save()
    print()
    print("=" * 72)
    print(f"  BOT STOPPED — {_now_utc()}")
    print(f"  Bankroll: ${state.bankroll:,.2f}  |  Trades: {state.total_trades}  |  WR: {state.win_rate:.1%}")
    print(f"  P&L: ${state.total_pnl:+,.2f}  |  Max DD: {state.max_drawdown:.1%}")
    print("=" * 72)
    print()


def _run_one_cycle(
    state: BotState,
    edge_threshold: float,
    max_bet_pct: float,
    kelly_mult: float,
    place_order,
) -> None:
    """Run a single trade cycle: wait → evaluate → trade → resolve."""

    # Determine the next window to trade
    next_ts = _next_window_ts()
    next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)

    # Skip if we already traded this window
    if next_ts <= state.last_window_ts:
        next_ts += WINDOW_SECONDS
        next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)

    # Wait until ORDER_LEAD_TIME seconds before window start
    target_time = next_ts - ORDER_LEAD_TIME
    now = time.time()
    if now < target_time:
        wait_secs = target_time - now
        _print(f"Next window: {next_dt.strftime('%H:%M:%S UTC')}  (waiting {wait_secs:.0f}s)")
        # Sleep in small chunks so we can respond to shutdown
        while time.time() < target_time:
            time.sleep(min(5.0, target_time - time.time()))

    # Evaluate signal (edge computed vs actual buy price including spread)
    model_up = conditional_prob_up(state.recent_outcomes)
    buy_price = DEFAULT_BUY_PRICE  # 0.510 (typical ask)
    edge = model_up - buy_price

    down_streak = 0
    for o in reversed(state.recent_outcomes):
        if o == "Down":
            down_streak += 1
        else:
            break

    _print(
        f"Window {next_dt.strftime('%H:%M')}  |  "
        f"P(Up)={model_up:.1%}  |  Edge={edge:+.1%}  |  "
        f"Prev: {' '.join(state.recent_outcomes[-3:])}  |  "
        f"Down streak: {down_streak}"
    )

    # Decide whether to trade
    if edge <= edge_threshold:
        _print(f"  No edge ({edge:+.1%} < {edge_threshold:.0%}). Skipping.")
        # Still need to wait for resolution to update outcomes
        _wait_and_update_outcomes(state, next_ts)
        return

    # Size the bet (fee-aware odds)
    net_odds = net_odds_after_fees(buy_price, is_maker=False)
    kf = kelly_fraction(model_up, net_odds, kelly_mult)
    cap = state.bankroll * max_bet_pct
    bet_size = min(state.bankroll * kf, cap, state.bankroll)

    if bet_size < MIN_BET:
        _print(f"  Bet too small (${bet_size:.2f} < ${MIN_BET}). Skipping.")
        _wait_and_update_outcomes(state, next_ts)
        return

    _print(f"  >>> BUY UP  |  Bet: ${bet_size:.2f}  |  Kelly: {kf:.1%}  |  Bank: ${state.bankroll:.2f}")

    # Place order (or simulate)
    if place_order and not state.dry_run:
        market = _fetch_5m_market(next_ts)
        if market and market.is_tradeable:
            from poly.model import evaluate_5m_market
            from poly.btc import get_snapshot

            snap = get_snapshot()
            sig = evaluate_5m_market(market, snap, edge_threshold, state.recent_outcomes)
            result = place_order(sig, market, bet_size)
            if result.success:
                _print(f"  ORDER PLACED: {result.order_id}")
            else:
                _print(f"  ORDER FAILED: {result.error}")
                _wait_and_update_outcomes(state, next_ts)
                return
        else:
            _print("  Market not found or not tradeable. Simulating.")
    else:
        _print(f"  [DRY RUN] Would place ${bet_size:.2f} on BUY UP")

    # Wait for resolution
    outcome = _wait_for_resolution(next_ts)
    if not outcome:
        _print("  Resolution timeout! Skipping this window.")
        state.last_window_ts = next_ts
        return

    # Calculate P&L
    won = outcome == "Up"
    if won:
        pnl = bet_size * net_odds
    else:
        pnl = -bet_size

    state.bankroll += pnl
    state.total_pnl += pnl
    state.total_trades += 1
    if won:
        state.total_wins += 1
    if state.bankroll > state.peak_bankroll:
        state.peak_bankroll = state.bankroll
    dd = state.current_drawdown
    if dd > state.max_drawdown:
        state.max_drawdown = dd

    # Update outcome history
    state.recent_outcomes.append(outcome)
    if len(state.recent_outcomes) > 5:
        state.recent_outcomes.pop(0)
    state.last_window_ts = next_ts

    # Log
    result_str = "WIN" if won else "LOSS"
    emoji = "+" if won else "-"
    _print(
        f"  {result_str}: {outcome}  |  PnL: ${pnl:+.2f}  |  "
        f"Bank: ${state.bankroll:.2f}  |  "
        f"WR: {state.win_rate:.1%} ({state.total_wins}/{state.total_trades})  |  "
        f"DD: {dd:.1%}"
    )

    _log_trade({
        "ts": next_ts,
        "time": datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat(),
        "side": "BUY UP",
        "model_p": model_up,
        "edge": edge,
        "bet": bet_size,
        "outcome": outcome,
        "won": won,
        "pnl": pnl,
        "bankroll": state.bankroll,
        "win_rate": state.win_rate,
        "drawdown": dd,
    })


def _wait_and_update_outcomes(state: BotState, window_ts: int) -> None:
    """Wait for a window to resolve and update outcome history (no trade)."""
    outcome = _wait_for_resolution(window_ts)
    if outcome:
        state.recent_outcomes.append(outcome)
        if len(state.recent_outcomes) > 5:
            state.recent_outcomes.pop(0)
        state.last_window_ts = window_ts
        _print(f"  Outcome: {outcome} (no trade)")
    else:
        _print("  Resolution timeout. Outcomes may be stale.")
        state.last_window_ts = window_ts


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Poly Autobot — automated 5-min BTC trading")
    parser.add_argument("-b", "--bankroll", type=float, default=500.0, help="Starting bankroll (default $500)")
    parser.add_argument("-e", "--edge", type=float, default=DEFAULT_EDGE_THRESHOLD, help="Edge threshold (default 0.03)")
    parser.add_argument("-k", "--kelly", type=float, default=KELLY_MULTIPLIER, help="Kelly multiplier (default 0.25)")
    parser.add_argument("--max-bet-pct", type=float, default=MAX_BET_PCT, help="Max bet as %% of bankroll (default 0.10)")
    parser.add_argument("--live", action="store_true", help="Place real orders (default: dry run)")
    parser.add_argument("--fresh", action="store_true", help="Start fresh (ignore saved state)")
    args = parser.parse_args()

    run_bot(
        bankroll=args.bankroll,
        edge_threshold=args.edge,
        max_bet_pct=args.max_bet_pct,
        kelly_mult=args.kelly,
        dry_run=not args.live,
        resume=not args.fresh,
    )


if __name__ == "__main__":
    main()
