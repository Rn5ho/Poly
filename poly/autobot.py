"""Automated trading bot for 5-minute BTC up/down markets.

Runs continuously, placing trades every 5-minute window based on
the conditional mean-reversion model. Tracks its own bankroll,
trade history, and performance metrics.

Key features:
  - Live book prices (no hardcoded bid/ask assumptions)
  - Fill verification for maker orders (poll until filled or cancel)
  - WebSocket-based stop-loss monitoring (with REST fallback)
  - Actual fill size used for P&L (not requested size)

Execution modes:
  --maker     Use maker (post-only) limit orders at bid ($0 fee)
  --taker     Use taker market orders at ask (1.56% fee, default)
  --stoploss  Enable stop-loss monitoring during live windows

Usage:
  python -m poly.autobot                       # dry run, $500 bankroll
  python -m poly.autobot --bankroll 1000       # dry run, $1000
  python -m poly.autobot --live --maker        # REAL, maker orders (recommended)
  python -m poly.autobot --live --stoploss     # REAL, with stop-loss

The bot:
1. Waits for each 5-minute window boundary
2. Fetches the most recent resolved outcomes
3. Fetches live order book for real bid/ask prices
4. Computes conditional P(Up) using graduated Down-streak model
5. If edge > threshold, sizes a quarter-Kelly bet and places it
6. Verifies fill (maker) — cancels unfilled orders before window start
7. Optionally: monitors live window for stop-loss (sell if Up < 30c)
8. Waits for resolution, updates bankroll using actual fill size, repeats
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
    MAKER_BUY_PRICE,
    MAX_BET_PCT,
    MIN_BET,
    STOP_LOSS_PRICE,
    conditional_prob_up,
    kelly_fraction,
    net_odds_after_fees,
)
from poly.notify import (
    is_configured as _tg_configured,
    notify_error,
    notify_fill,
    notify_outcome,
    notify_shutdown,
    notify_startup,
    notify_stoploss,
    notify_trade_placed,
    send_daily_summary,
)
from poly.polymarket import (
    WINDOW_SECONDS,
    _fetch_5m_market,
    _fetch_resolved_outcome,
    fetch_live_book,
    fetch_order_book,
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
# How often to check price during stop-loss monitoring
STOPLOSS_POLL = 5

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
    total_stopouts: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    recent_outcomes: list[str] = field(default_factory=list)
    last_window_ts: int = 0
    started_at: str = ""
    dry_run: bool = True
    use_maker: bool = False

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
            # Handle legacy state files missing new fields
            valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
            data = {k: v for k, v in data.items() if k in valid_fields}
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


def _get_up_price_from_clob(token_id: str) -> float | None:
    """Get current best bid for Up token from CLOB order book."""
    try:
        book = fetch_order_book(token_id)
        bids = book.get("bids", [])
        if bids:
            # Best bid is the highest price
            return max(float(b["price"]) for b in bids)
        return None
    except Exception:
        return None


def _monitor_stoploss(
    window_ts: int,
    up_token_id: str,
    shares_held: float,
    buy_price: float,
    bet_size: float,
    dry_run: bool,
    condition_id: str | None = None,
) -> dict | None:
    """Monitor live window for stop-loss trigger.

    Tries WebSocket streaming first for sub-second price updates.
    Falls back to REST polling if WebSocket is unavailable.

    Args:
        window_ts: Window start timestamp.
        up_token_id: CLOB token ID for Up shares.
        shares_held: Actual number of shares held (from fill verification).
        buy_price: Price paid per share.
        bet_size: Total USD spent.
        dry_run: If True, simulate sells.
        condition_id: Market condition ID (for WebSocket subscription).

    Returns:
        dict with stop-loss details if triggered, None otherwise.
    """
    window_end = window_ts + WINDOW_SECONDS

    # Try WebSocket-based monitoring first
    stream = _try_start_market_stream(up_token_id, condition_id)
    use_ws = stream is not None

    try:
        while time.time() < window_end - 10:  # stop 10s before end
            # Get price from WebSocket or REST fallback
            if use_ws and stream.last_price is not None:
                current_price = stream.last_price
            else:
                current_price = _get_up_price_from_clob(up_token_id)

            if current_price is not None and current_price < STOP_LOSS_PRICE:
                _print(
                    f"    STOP-LOSS TRIGGERED: Up={current_price:.2f} < {STOP_LOSS_PRICE:.2f}"
                )

                # Execute sell
                if not dry_run:
                    from poly.execute import sell_shares
                    result = sell_shares(
                        up_token_id, shares_held, price=current_price, as_maker=False
                    )
                    if result.success:
                        _print(f"    SOLD: {result.order_id}")
                    else:
                        _print(f"    SELL FAILED: {result.error}")
                        # Retry once as taker market sell (no price)
                        result = sell_shares(
                            up_token_id, shares_held, price=None, as_maker=False
                        )
                        if result.success:
                            _print(f"    SOLD (retry): {result.order_id}")
                        else:
                            _print(f"    SELL RETRY FAILED: {result.error}")
                            return None
                else:
                    _print(
                        f"    [DRY RUN] Would sell {shares_held:.1f} shares at {current_price:.2f}"
                    )

                recovery = shares_held * current_price
                pnl = recovery - bet_size
                return {
                    "stopped": True,
                    "exit_price": current_price,
                    "pnl": pnl,
                    "recovery_pct": recovery / bet_size if bet_size > 0 else 0,
                }

            # WebSocket provides sub-second updates; REST needs explicit sleep
            if not use_ws or stream.last_price is None:
                time.sleep(STOPLOSS_POLL)
            else:
                time.sleep(1)  # light sleep between WS checks
    finally:
        if stream:
            stream.stop()

    return None  # no stop-loss triggered


def _try_start_market_stream(token_id: str, condition_id: str | None = None):
    """Try to start a WebSocket market stream. Returns None if unavailable."""
    try:
        from poly.ws import MarketStream
        stream = MarketStream(token_id, condition_id)
        stream.start()
        # Give it a moment to connect
        time.sleep(1)
        return stream
    except Exception:
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
    use_maker: bool = False,
    enable_stoploss: bool = False,
) -> None:
    """Run the automated trading bot.

    Args:
        bankroll: Starting bankroll in USD.
        edge_threshold: Minimum edge to trade (default 3%).
        max_bet_pct: Max bet as fraction of bankroll (default 10%).
        kelly_mult: Kelly multiplier (default 0.25 = quarter-Kelly).
        dry_run: If True, simulate trades without placing orders.
        resume: If True, resume from saved state.
        use_maker: If True, use maker (post-only) limit orders ($0 fee).
        enable_stoploss: If True, monitor live windows for stop-loss.
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
            use_maker=use_maker,
        )

    state.dry_run = dry_run
    state.use_maker = use_maker
    mode = "DRY RUN" if dry_run else "LIVE"
    order_type = "MAKER ($0 fee)" if use_maker else "TAKER (1.56% fee)"
    stoploss_str = f"  |  Stop-loss: {STOP_LOSS_PRICE:.0%}" if enable_stoploss else ""

    # Lazy import for live trading
    place_order_fn = None
    sell_fn = None
    if not dry_run:
        from poly.execute import place_maker_order, place_market_order, sell_shares
        place_order_fn = place_maker_order if use_maker else place_market_order
        sell_fn = sell_shares

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
    print(f"  POLY AUTOBOT ({mode}) — {order_type}")
    print(f"  Bankroll: ${state.bankroll:,.2f}  |  Kelly: {kelly_mult:.0%}  |  Edge: {edge_threshold:.0%}{stoploss_str}")
    if _tg_configured():
        print(f"  Telegram: ON")
    print(f"  Started: {state.started_at}")
    if state.total_trades > 0:
        print(f"  Trades: {state.total_trades}  |  WR: {state.win_rate:.1%}  |  P&L: ${state.total_pnl:+,.2f}")
    print("=" * 72)
    print()

    # Notify startup
    notify_startup(mode, order_type, state.bankroll, kelly_mult, edge_threshold, enable_stoploss)

    # Bootstrap recent outcomes if we don't have them
    if not state.recent_outcomes:
        _print("Fetching recent outcomes for conditional model...")
        state.recent_outcomes = fetch_recent_outcomes(n=5)
        if state.recent_outcomes:
            _print(f"  Recent: {' → '.join(state.recent_outcomes)}")
        else:
            _print("  No recent outcomes (will use base rate)")

    # Daily summary tracking
    last_daily_summary = 0

    # Main loop
    while not shutdown:
        try:
            _run_one_cycle(
                state, edge_threshold, max_bet_pct, kelly_mult,
                place_order_fn, sell_fn, use_maker, enable_stoploss,
            )
            state.save()

            # Send daily summary at ~00:00 UTC
            now_ts = int(time.time())
            today_midnight = (now_ts // 86400) * 86400
            if today_midnight > last_daily_summary:
                send_daily_summary()
                last_daily_summary = today_midnight
        except KeyboardInterrupt:
            break
        except Exception as e:
            _print(f"ERROR: {e}")
            notify_error(str(e))
            time.sleep(30)  # back off on errors

    # Final save
    state.save()
    notify_shutdown(state.bankroll, state.total_trades, state.win_rate, state.total_pnl, state.max_drawdown)
    print()
    print("=" * 72)
    print(f"  BOT STOPPED — {_now_utc()}")
    print(f"  Bankroll: ${state.bankroll:,.2f}  |  Trades: {state.total_trades}  |  WR: {state.win_rate:.1%}")
    print(f"  P&L: ${state.total_pnl:+,.2f}  |  Max DD: {state.max_drawdown:.1%}  |  Stopouts: {state.total_stopouts}")
    print("=" * 72)
    print()


def _run_one_cycle(
    state: BotState,
    edge_threshold: float,
    max_bet_pct: float,
    kelly_mult: float,
    place_order_fn,
    sell_fn,
    use_maker: bool,
    enable_stoploss: bool,
) -> None:
    """Run a single trade cycle: wait → book → evaluate → trade → verify → monitor → resolve."""

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
        while time.time() < target_time:
            time.sleep(min(5.0, target_time - time.time()))

    # Fetch market for token IDs and live book
    market = _fetch_5m_market(next_ts)
    up_token_id = None
    condition_id = None
    live_bid = 0.0
    live_ask = 0.0

    if market and market.is_tradeable:
        up_token_id = market.up_token_id
        condition_id = market.condition_id

        # Fetch live book prices instead of using hardcoded constants
        book = fetch_live_book(up_token_id)
        if book and book.is_valid:
            live_bid = book.best_bid
            live_ask = book.best_ask
            _print(f"  Book: bid={live_bid:.3f} ({book.bid_size:.0f} shares)  ask={live_ask:.3f} ({book.ask_size:.0f} shares)  spread={book.spread:.3f}")
        else:
            _print(f"  Book unavailable, using defaults")

    # Determine buy price: live book > fallback constants
    if use_maker:
        buy_price = live_bid if live_bid > 0 else MAKER_BUY_PRICE
    else:
        buy_price = live_ask if live_ask > 0 else DEFAULT_BUY_PRICE

    # Evaluate signal
    model_up = conditional_prob_up(state.recent_outcomes)
    edge = model_up - buy_price

    down_streak = 0
    for o in reversed(state.recent_outcomes):
        if o == "Down":
            down_streak += 1
        else:
            break

    order_label = "MAKER" if use_maker else "TAKER"
    _print(
        f"Window {next_dt.strftime('%H:%M')}  |  "
        f"P(Up)={model_up:.1%}  |  Buy={buy_price:.3f}  |  Edge={edge:+.1%} ({order_label})  |  "
        f"Prev: {' '.join(state.recent_outcomes[-3:])}  |  "
        f"Down streak: {down_streak}"
    )

    # Decide whether to trade
    if edge <= edge_threshold:
        _print(f"  No edge ({edge:+.1%} < {edge_threshold:.0%}). Skipping.")
        _wait_and_update_outcomes(state, next_ts)
        return

    # Size the bet using live price
    net_odds = net_odds_after_fees(buy_price, is_maker=use_maker)
    kf = kelly_fraction(model_up, net_odds, kelly_mult)
    cap = state.bankroll * max_bet_pct
    bet_size = min(state.bankroll * kf, cap, state.bankroll)

    if bet_size < MIN_BET:
        _print(f"  Bet too small (${bet_size:.2f} < ${MIN_BET}). Skipping.")
        _wait_and_update_outcomes(state, next_ts)
        return

    _print(f"  >>> BUY UP ({order_label})  |  Bet: ${bet_size:.2f}  |  Kelly: {kf:.1%}  |  Bank: ${state.bankroll:.2f}")

    # Telegram: trade placed
    window_time = datetime.fromtimestamp(next_ts, tz=timezone.utc).strftime("%H:%M UTC")
    notify_trade_placed(
        window_time=window_time, side="BUY UP", order_type=order_label,
        bet_size=bet_size, buy_price=buy_price, model_prob=model_up,
        edge=edge, bankroll=state.bankroll, down_streak=down_streak,
    )

    # Place order and verify fill
    shares_requested = bet_size / buy_price
    actual_shares = shares_requested  # will be updated by fill verification
    actual_bet = bet_size  # will be updated by fill verification
    fill_fraction = 1.0
    order_id = None

    if place_order_fn and not state.dry_run:
        if not market or not market.is_tradeable:
            _print("  Market not found or not tradeable. Simulating.")
        else:
            if use_maker:
                # Maker: post-only limit buy at live bid price
                result = place_order_fn(
                    token_id=up_token_id,
                    price=buy_price,
                    size=round(shares_requested, 2),
                    side="BUY",
                )
            else:
                # Taker: market order
                from poly.model import evaluate_5m_market
                from poly.btc import get_snapshot
                snap = get_snapshot()
                sig = evaluate_5m_market(market, snap, edge_threshold, state.recent_outcomes)
                result = place_order_fn(sig, market, bet_size)

            if not result.success:
                _print(f"  ORDER FAILED: {result.error}")
                _wait_and_update_outcomes(state, next_ts)
                return

            order_id = result.order_id
            _print(f"  ORDER PLACED: {order_id}")

            # Fill verification for maker orders
            if use_maker and order_id:
                from poly.execute import wait_for_fill
                # Wait up to ORDER_LEAD_TIME for fill, then cancel unfilled
                fill_timeout = max(ORDER_LEAD_TIME - 5, 10)
                _print(f"    Waiting up to {fill_timeout}s for fill...")
                fill_status = wait_for_fill(
                    order_id,
                    timeout=fill_timeout,
                    poll_interval=2.0,
                    cancel_on_timeout=True,
                )

                if fill_status is None:
                    _print(f"    Could not verify fill status. Assuming no fill.")
                    _wait_and_update_outcomes(state, next_ts)
                    return

                actual_shares = fill_status.size_matched
                fill_fraction = fill_status.fill_fraction

                if actual_shares <= 0:
                    _print(f"    No fill — order {fill_status.status}. Skipping trade.")
                    _wait_and_update_outcomes(state, next_ts)
                    return

                actual_bet = actual_shares * buy_price
                if fill_status.is_fully_filled:
                    _print(f"    FILLED: {actual_shares:.1f} shares (100%)")
                else:
                    _print(
                        f"    PARTIAL FILL: {actual_shares:.1f}/{shares_requested:.1f} shares "
                        f"({fill_fraction:.0%}) — unfilled portion canceled"
                    )
                    # Adjust bet_size to actual filled amount
                    bet_size = actual_bet

                notify_fill(order_id, actual_shares, shares_requested, fill_fraction)
    else:
        _print(f"  [DRY RUN] Would place ${bet_size:.2f} on BUY UP ({order_label})")

    # Stop-loss monitoring during live window
    stopped_out = False
    stoploss_result = None
    if enable_stoploss and up_token_id and actual_shares > 0:
        _print(f"    Monitoring stop-loss (sell if Up < {STOP_LOSS_PRICE:.0%})...")
        stoploss_result = _monitor_stoploss(
            next_ts, up_token_id, actual_shares, buy_price, bet_size,
            dry_run=state.dry_run, condition_id=condition_id,
        )
        if stoploss_result and stoploss_result.get("stopped"):
            stopped_out = True

    if stopped_out:
        pnl = stoploss_result["pnl"]
        state.total_stopouts += 1
        won = False
        outcome_label = "STOP-LOSS"
        notify_stoploss(
            window_time=window_time,
            exit_price=stoploss_result.get("exit_price", 0),
            pnl=pnl,
            recovery_pct=stoploss_result.get("recovery_pct", 0),
        )
    else:
        # Wait for resolution
        outcome = _wait_for_resolution(next_ts)
        if not outcome:
            _print("  Resolution timeout! Skipping this window.")
            state.last_window_ts = next_ts
            return

        # Calculate P&L using actual fill size
        won = outcome == "Up"
        if won:
            pnl = bet_size * net_odds
        else:
            pnl = -bet_size
        outcome_label = outcome

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

    # Update outcome history (always need actual outcome for conditional model)
    if not stopped_out:
        state.recent_outcomes.append(outcome)
        if len(state.recent_outcomes) > 5:
            state.recent_outcomes.pop(0)
    else:
        actual_outcome = _wait_for_resolution(next_ts)
        if actual_outcome:
            state.recent_outcomes.append(actual_outcome)
            if len(state.recent_outcomes) > 5:
                state.recent_outcomes.pop(0)

    state.last_window_ts = next_ts

    # Log with fill details
    result_str = "WIN" if won else ("STOP" if stopped_out else "LOSS")
    _print(
        f"  {result_str}: {outcome_label}  |  PnL: ${pnl:+.2f}  |  "
        f"Bank: ${state.bankroll:.2f}  |  "
        f"WR: {state.win_rate:.1%} ({state.total_wins}/{state.total_trades})  |  "
        f"DD: {dd:.1%}"
    )

    # Telegram: outcome
    notify_outcome(
        window_time=window_time, outcome=outcome_label, won=won,
        pnl=pnl, bankroll=state.bankroll, win_rate=state.win_rate,
        total_trades=state.total_trades, drawdown=dd, stopped_out=stopped_out,
    )

    _log_trade({
        "ts": next_ts,
        "time": datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat(),
        "side": "BUY UP",
        "order_type": "MAKER" if use_maker else "TAKER",
        "order_id": order_id,
        "model_p": model_up,
        "buy_price": buy_price,
        "live_bid": live_bid,
        "live_ask": live_ask,
        "edge": edge,
        "bet": bet_size,
        "shares_requested": round(shares_requested, 2),
        "shares_filled": round(actual_shares, 2),
        "fill_fraction": round(fill_fraction, 4),
        "outcome": outcome_label,
        "won": won,
        "stopped_out": stopped_out,
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
    parser.add_argument("--maker", action="store_true", help="Use maker orders ($0 fee, recommended)")
    parser.add_argument("--taker", action="store_true", help="Use taker orders (1.56% fee)")
    parser.add_argument("--stoploss", action="store_true", help="Enable stop-loss monitoring during live windows")
    args = parser.parse_args()

    # Default to maker if neither specified
    use_maker = args.maker or not args.taker

    run_bot(
        bankroll=args.bankroll,
        edge_threshold=args.edge,
        max_bet_pct=args.max_bet_pct,
        kelly_mult=args.kelly,
        dry_run=not args.live,
        resume=not args.fresh,
        use_maker=use_maker,
        enable_stoploss=args.stoploss,
    )


if __name__ == "__main__":
    main()
