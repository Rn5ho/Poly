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
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from poly.model import (
    ARB_BET_PCT,
    ARB_MAX_COMBINED,
    ARB_MAX_PER_WINDOW,
    ARB_MONITOR_SECS,
    DEFAULT_BUY_PRICE,
    DEFAULT_EDGE_THRESHOLD,
    DIP_ENTRY_PRICE,
    DIP_LEVELS,
    DIP_MAX_PER_WINDOW,
    DIP_MONITOR_SECS,
    KELLY_MULTIPLIER,
    MAKER_BUY_PRICE,
    MAX_BET_PCT,
    MIN_BET,
    STOP_LOSS_PRICE,
    conditional_prob_up,
    kelly_fraction,
    net_odds_after_fees,
    taker_fee_rate,
)
from poly.notify import (
    is_configured as _tg_configured,
    notify_arb_buy,
    notify_dip_buy,
    notify_dip_outcome,
    notify_error,
    notify_fill,
    notify_outcome,
    notify_shutdown,
    notify_startup,
    notify_stoploss,
    notify_trade_placed,
    send_daily_summary,
    start_command_handler,
    stop_command_handler,
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


def _wait_for_resolution(
    window_ts: int,
    shutdown_event: threading.Event | None = None,
) -> str | None:
    """Wait for a window to resolve and return the outcome."""
    window_end = window_ts + WINDOW_SECONDS
    # Wait until window ends + buffer
    now = time.time()
    if now < window_end + RESOLUTION_WAIT:
        wait = window_end + RESOLUTION_WAIT - now
        _print(f"    Waiting {wait:.0f}s for resolution...")
        if shutdown_event:
            if shutdown_event.wait(wait):
                return None
        else:
            time.sleep(wait)

    # Poll for resolution
    deadline = time.time() + RESOLUTION_TIMEOUT
    while time.time() < deadline:
        if shutdown_event and shutdown_event.is_set():
            return None
        outcome = _fetch_resolved_outcome(window_ts)
        if outcome:
            return outcome
        if shutdown_event:
            shutdown_event.wait(RESOLUTION_POLL)
        else:
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


def _get_best_ask_from_clob(token_id: str) -> float | None:
    """Get current best ask for a token from CLOB order book."""
    try:
        book = fetch_order_book(token_id)
        asks = book.get("asks", [])
        if asks:
            return min(float(a["price"]) for a in asks)
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
    """Print with timestamp. Always flushes for systemd/pipe compatibility."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


def run_bot(
    bankroll: float = 500.0,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    max_bet_pct: float = MAX_BET_PCT,
    kelly_mult: float = KELLY_MULTIPLIER,
    dry_run: bool = True,
    resume: bool = True,
    use_maker: bool = False,
    enable_stoploss: bool = False,
    mode: str = "default",
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
        mode: Trading mode — "default" (pre-window), "dip", or "arb".
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
    state.save()  # Write initial state so dashboard has data immediately
    run_mode = "DRY RUN" if dry_run else "LIVE"

    if mode == "dip":
        order_type = "DIP-BUY (mid-window)"
    elif mode == "arb":
        order_type = "DUAL-SIDE ARB"
    elif use_maker:
        order_type = "MAKER ($0 fee)"
    else:
        order_type = "TAKER (1.56% fee)"
    stoploss_str = f"  |  Stop-loss: {STOP_LOSS_PRICE:.0%}" if enable_stoploss else ""

    # Lazy import for live trading (default mode only)
    place_order_fn = None
    sell_fn = None
    if not dry_run and mode == "default":
        from poly.execute import place_maker_order, place_market_order, sell_shares
        place_order_fn = place_maker_order if use_maker else place_market_order
        sell_fn = sell_shares

    # Handle graceful shutdown — uses Event so sleeps can be interrupted
    shutdown_event = threading.Event()

    def _shutdown(sig, frame):
        shutdown_event.set()
        _print("Shutting down gracefully...")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Print banner
    print(flush=True)
    print("=" * 72, flush=True)
    print(f"  POLY AUTOBOT ({run_mode}) — {order_type}", flush=True)
    print(f"  Bankroll: ${state.bankroll:,.2f}  |  Kelly: {kelly_mult:.0%}  |  Edge: {edge_threshold:.0%}{stoploss_str}", flush=True)
    if _tg_configured():
        print(f"  Telegram: ON", flush=True)
    print(f"  Started: {state.started_at}", flush=True)
    if state.total_trades > 0:
        print(f"  Trades: {state.total_trades}  |  WR: {state.win_rate:.1%}  |  P&L: ${state.total_pnl:+,.2f}", flush=True)
    print("=" * 72, flush=True)
    print(flush=True)

    # Notify startup + start Telegram command handler
    notify_startup(run_mode, order_type, state.bankroll, kelly_mult, edge_threshold, enable_stoploss)
    start_command_handler()

    # Bootstrap recent outcomes if we don't have them
    if not state.recent_outcomes:
        _print("Fetching recent outcomes for conditional model...")
        state.recent_outcomes = fetch_recent_outcomes(n=5)
        if state.recent_outcomes:
            _print(f"  Recent: {' → '.join(state.recent_outcomes)}")
        else:
            _print("  No recent outcomes (will use base rate)")

    # Daily summary tracking — init to today's midnight so we don't fire on startup
    last_daily_summary = (int(time.time()) // 86400) * 86400

    # Main loop
    while not shutdown_event.is_set():
        try:
            if mode == "dip":
                _run_dip_cycle(state, dry_run, shutdown_event)
            elif mode == "arb":
                _run_arb_cycle(state, dry_run, shutdown_event)
            else:
                _run_one_cycle(
                    state, edge_threshold, max_bet_pct, kelly_mult,
                    place_order_fn, sell_fn, use_maker, enable_stoploss,
                    shutdown_event,
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
            shutdown_event.wait(30)  # back off on errors (interruptible)

    # Final save
    stop_command_handler()
    state.save()
    notify_shutdown(state.bankroll, state.total_trades, state.win_rate, state.total_pnl, state.max_drawdown)
    print(flush=True)
    print("=" * 72, flush=True)
    print(f"  BOT STOPPED — {_now_utc()}", flush=True)
    print(f"  Bankroll: ${state.bankroll:,.2f}  |  Trades: {state.total_trades}  |  WR: {state.win_rate:.1%}", flush=True)
    print(f"  P&L: ${state.total_pnl:+,.2f}  |  Max DD: {state.max_drawdown:.1%}  |  Stopouts: {state.total_stopouts}", flush=True)
    print("=" * 72, flush=True)
    print(flush=True)


def _catch_up_outcomes(state: BotState) -> None:
    """Fetch outcomes for any windows between last processed and now.

    The bot takes ~6 minutes per cycle (5-min window + 60s resolution wait),
    so it can fall behind and miss windows. This fills in the gaps so that
    recent_outcomes always reflects the true sequence.
    """
    if not state.last_window_ts:
        return

    now = int(time.time())
    current_window = (now // WINDOW_SECONDS) * WINDOW_SECONDS

    # Collect windows that have ended but weren't processed
    missed = []
    ts = state.last_window_ts + WINDOW_SECONDS
    while ts < current_window:  # only windows that have fully ended
        missed.append(ts)
        ts += WINDOW_SECONDS

    if not missed:
        return

    _print(f"  Catching up {len(missed)} missed window(s)...")
    for ts in missed:
        outcome = _fetch_resolved_outcome(ts)
        if outcome:
            state.recent_outcomes.append(outcome)
            if len(state.recent_outcomes) > 5:
                state.recent_outcomes.pop(0)
            state.last_window_ts = ts
        else:
            # Window not yet resolved — stop catching up here
            break

    if missed:
        _print(f"  Outcomes now: {' → '.join(state.recent_outcomes[-5:])}")


def _run_one_cycle(
    state: BotState,
    edge_threshold: float,
    max_bet_pct: float,
    kelly_mult: float,
    place_order_fn,
    sell_fn,
    use_maker: bool,
    enable_stoploss: bool,
    shutdown_event: threading.Event | None = None,
) -> None:
    """Run a single trade cycle: wait → book → evaluate → trade → verify → monitor → resolve."""

    # Catch up on any missed windows before evaluating
    _catch_up_outcomes(state)

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
            if shutdown_event and shutdown_event.wait(min(5.0, max(0, target_time - time.time()))):
                return  # Exit immediately on shutdown
            elif not shutdown_event:
                time.sleep(min(5.0, max(0, target_time - time.time())))

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
        _wait_and_update_outcomes(state, next_ts, shutdown_event)
        return

    # Size the bet using live price
    net_odds = net_odds_after_fees(buy_price, is_maker=use_maker)
    kf = kelly_fraction(model_up, net_odds, kelly_mult)
    cap = state.bankroll * max_bet_pct
    bet_size = min(state.bankroll * kf, cap, state.bankroll)

    if bet_size < MIN_BET:
        _print(f"  Bet too small (${bet_size:.2f} < ${MIN_BET}). Skipping.")
        _wait_and_update_outcomes(state, next_ts, shutdown_event)
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
                notify_error(f"Order failed for {window_time}: {result.error}")
                _wait_and_update_outcomes(state, next_ts, shutdown_event)
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
                    notify_fill(order_id, 0, shares_requested, 0.0)
                    _wait_and_update_outcomes(state, next_ts, shutdown_event)
                    return

                actual_shares = fill_status.size_matched
                fill_fraction = fill_status.fill_fraction

                if actual_shares <= 0:
                    _print(f"    No fill — order {fill_status.status}. Skipping trade.")
                    notify_fill(order_id, 0, shares_requested, 0.0)
                    _wait_and_update_outcomes(state, next_ts, shutdown_event)
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
        outcome = _wait_for_resolution(next_ts, shutdown_event)
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
        actual_outcome = _wait_for_resolution(next_ts, shutdown_event)
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


def _wait_and_update_outcomes(
    state: BotState,
    window_ts: int,
    shutdown_event: threading.Event | None = None,
) -> None:
    """Wait for a window to resolve and update outcome history (no trade)."""
    outcome = _wait_for_resolution(window_ts, shutdown_event)
    if outcome:
        state.recent_outcomes.append(outcome)
        if len(state.recent_outcomes) > 5:
            state.recent_outcomes.pop(0)
        state.last_window_ts = window_ts
        _print(f"  Outcome: {outcome} (no trade)")
    else:
        _print("  Resolution timeout. Outcomes may be stale.")
        state.last_window_ts = window_ts


def _run_dip_cycle(
    state: BotState,
    dry_run: bool,
    shutdown_event: threading.Event | None = None,
) -> None:
    """Run a dip-buy cycle: wait for window start -> monitor price -> buy dips -> resolve.

    During the live 5-minute window, monitors the Up token price.
    When it drops below DIP_LEVELS thresholds, places taker buy orders.
    Taker fees at low prices are negligible (~0.16% at 22c).
    Holds all shares to resolution.
    """
    # Catch up on any missed windows
    _catch_up_outcomes(state)

    # Determine the next window
    next_ts = _next_window_ts()
    next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
    window_time = next_dt.strftime("%H:%M UTC")

    if next_ts <= state.last_window_ts:
        next_ts += WINDOW_SECONDS
        next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        window_time = next_dt.strftime("%H:%M UTC")

    # Wait until window STARTS (not before — we buy during live action)
    now = time.time()
    if now < next_ts:
        wait_secs = next_ts - now
        _print(f"Next window: {next_dt.strftime('%H:%M:%S UTC')}  (waiting {wait_secs:.0f}s for start)")
        while time.time() < next_ts:
            if shutdown_event and shutdown_event.wait(min(5.0, max(0, next_ts - time.time()))):
                return

    # Fetch market for token IDs
    market = _fetch_5m_market(next_ts)
    if not market or not market.up_token_id:
        _print(f"  Market not found for {window_time}. Skipping.")
        _wait_and_update_outcomes(state, next_ts, shutdown_event)
        return

    up_token_id = market.up_token_id
    condition_id = market.condition_id
    _print(f"Window {window_time}  |  DIP MODE  |  Monitoring Up token for dips...")

    # Monitor and buy dips during live window
    monitor_end = next_ts + DIP_MONITOR_SECS
    window_end = next_ts + WINDOW_SECONDS
    triggered_levels: set[float] = set()
    buys: list[dict] = []
    total_spent = 0.0
    max_spend = state.bankroll * DIP_MAX_PER_WINDOW

    # Start WebSocket stream for sub-second price updates
    stream = _try_start_market_stream(up_token_id, condition_id)
    use_ws = stream is not None

    try:
        while time.time() < min(monitor_end, window_end - 30):
            if shutdown_event and shutdown_event.is_set():
                return

            # Get current Up price
            current_price = None
            if use_ws and stream and stream.last_price is not None:
                current_price = stream.last_price
            else:
                current_price = _get_up_price_from_clob(up_token_id)

            if current_price is None:
                time.sleep(2)
                continue

            # Check each dip level
            for level_price, bet_pct in DIP_LEVELS:
                if level_price in triggered_levels:
                    continue
                if current_price > level_price:
                    continue
                if total_spent >= max_spend:
                    break

                bet_size = min(
                    state.bankroll * bet_pct,
                    max_spend - total_spent,
                )
                if bet_size < MIN_BET:
                    continue

                shares = bet_size / current_price
                fee_pct = taker_fee_rate(current_price) * 100
                odds = 1.0 / current_price - 1.0

                _print(
                    f"  >>> DIP BUY @ {current_price:.3f}  |  "
                    f"${bet_size:.2f} → {shares:.1f} shares  |  "
                    f"Odds: {odds:.1f}:1  |  Fee: {fee_pct:.2f}%  |  "
                    f"Level: ≤{level_price:.2f}"
                )

                # Place order
                order_id = None
                if not dry_run:
                    from poly.execute import place_taker_buy
                    result = place_taker_buy(up_token_id, bet_size)
                    if result.success:
                        order_id = result.order_id
                        _print(f"    ORDER: {order_id}")
                    else:
                        _print(f"    ORDER FAILED: {result.error}")
                        notify_error(f"Dip buy failed at {current_price:.3f}: {result.error}")
                        continue
                else:
                    _print(f"    [DRY RUN] Would buy ${bet_size:.2f} of Up @ {current_price:.3f}")

                buys.append({
                    "price": current_price,
                    "size": bet_size,
                    "shares": shares,
                    "level": level_price,
                    "order_id": order_id,
                })
                total_spent += bet_size
                triggered_levels.add(level_price)

                notify_dip_buy(
                    window_time=window_time,
                    price=current_price,
                    bet_size=bet_size,
                    shares=shares,
                    level=level_price,
                    total_spent=total_spent,
                    bankroll=state.bankroll,
                )

            # Sleep between price checks
            if not use_ws or (stream and stream.last_price is None):
                time.sleep(STOPLOSS_POLL)
            else:
                time.sleep(1)
    finally:
        if stream:
            stream.stop()

    if not buys:
        _print(f"  No dips below {DIP_ENTRY_PRICE:.2f} this window.")
        _wait_and_update_outcomes(state, next_ts, shutdown_event)
        return

    # Wait for resolution
    outcome = _wait_for_resolution(next_ts, shutdown_event)
    if not outcome:
        _print("  Resolution timeout!")
        state.last_window_ts = next_ts
        return

    # Calculate P&L across all dip buys
    total_shares = sum(b["shares"] for b in buys)
    total_cost = sum(b["size"] for b in buys)
    avg_price = total_cost / total_shares if total_shares > 0 else 0

    if outcome == "Up":
        # Each share pays $1.00 at resolution (minus entry fee, already deducted)
        avg_fee = taker_fee_rate(avg_price)
        effective_shares = total_shares * (1 - avg_fee)
        pnl = effective_shares - total_cost
        won = True
    else:
        pnl = -total_cost
        won = False

    # Update state
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

    state.recent_outcomes.append(outcome)
    if len(state.recent_outcomes) > 5:
        state.recent_outcomes.pop(0)
    state.last_window_ts = next_ts

    result_str = "WIN" if won else "LOSS"
    _print(
        f"  {result_str}: {outcome}  |  {len(buys)} buys @ avg {avg_price:.3f}  |  "
        f"PnL: ${pnl:+.2f}  |  Bank: ${state.bankroll:.2f}  |  "
        f"WR: {state.win_rate:.1%}"
    )

    notify_dip_outcome(
        window_time=window_time, outcome=outcome, num_buys=len(buys),
        total_cost=total_cost, total_pnl=pnl, bankroll=state.bankroll,
        win_rate=state.win_rate, total_trades=state.total_trades, mode="DIP",
    )

    _log_trade({
        "ts": next_ts,
        "time": datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat(),
        "side": "DIP BUY UP",
        "order_type": "DIP",
        "model_p": 0,
        "buy_price": avg_price,
        "edge": 0,
        "bet": total_cost,
        "shares_requested": round(total_shares, 2),
        "shares_filled": round(total_shares, 2),
        "fill_fraction": 1.0,
        "outcome": outcome,
        "won": won,
        "stopped_out": False,
        "pnl": pnl,
        "bankroll": state.bankroll,
        "win_rate": state.win_rate,
        "drawdown": dd,
        "dip_buys": buys,
    })


def _run_arb_cycle(
    state: BotState,
    dry_run: bool,
    shutdown_event: threading.Event | None = None,
) -> None:
    """Run a dual-side arb cycle: buy both Up+Down when combined < threshold.

    Monitors both Up and Down token prices during the live window.
    When combined price < ARB_MAX_COMBINED, buys both sides.
    Guaranteed profit = $1.00 - combined_cost per share-pair.
    """
    # Catch up on missed windows
    _catch_up_outcomes(state)

    # Next window
    next_ts = _next_window_ts()
    next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
    window_time = next_dt.strftime("%H:%M UTC")

    if next_ts <= state.last_window_ts:
        next_ts += WINDOW_SECONDS
        next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        window_time = next_dt.strftime("%H:%M UTC")

    # Wait for window START
    now = time.time()
    if now < next_ts:
        wait_secs = next_ts - now
        _print(f"Next window: {next_dt.strftime('%H:%M:%S UTC')}  (waiting {wait_secs:.0f}s for start)")
        while time.time() < next_ts:
            if shutdown_event and shutdown_event.wait(min(5.0, max(0, next_ts - time.time()))):
                return

    # Fetch market for both token IDs
    market = _fetch_5m_market(next_ts)
    if not market or not market.up_token_id or not market.down_token_id:
        _print(f"  Market not found for {window_time}. Skipping.")
        _wait_and_update_outcomes(state, next_ts, shutdown_event)
        return

    up_token_id = market.up_token_id
    down_token_id = market.down_token_id
    _print(f"Window {window_time}  |  ARB MODE  |  Monitoring Up+Down prices...")

    # Monitor for arb opportunities
    monitor_end = next_ts + ARB_MONITOR_SECS
    window_end = next_ts + WINDOW_SECONDS
    arb_buys: list[dict] = []
    total_spent = 0.0
    max_spend = state.bankroll * ARB_MAX_PER_WINDOW

    while time.time() < min(monitor_end, window_end - 30):
        if shutdown_event and shutdown_event.is_set():
            return

        # Get ask prices for both sides (we're buying taker, so we pay the ask)
        up_price = _get_best_ask_from_clob(up_token_id)
        down_price = _get_best_ask_from_clob(down_token_id)

        if up_price is None or down_price is None:
            time.sleep(STOPLOSS_POLL)
            continue

        combined = up_price + down_price

        if combined < ARB_MAX_COMBINED and total_spent < max_spend:
            gap = 1.0 - combined
            guaranteed_pct = gap / combined * 100

            bet_size = min(
                state.bankroll * ARB_BET_PCT,
                max_spend - total_spent,
            )
            if bet_size < MIN_BET * 2:  # need enough for both sides
                time.sleep(STOPLOSS_POLL)
                continue

            # Split bet proportionally: more on the cheaper side (higher return)
            # Equal share-pairs: buy N shares of each side
            # Cost per pair = up_price + down_price, payout = $1.00
            share_pairs = bet_size / combined
            up_cost = share_pairs * up_price
            down_cost = share_pairs * down_price

            _print(
                f"  >>> ARB @ combined={combined:.3f}  |  "
                f"Up={up_price:.3f} (${up_cost:.2f})  Down={down_price:.3f} (${down_cost:.2f})  |  "
                f"Gap: {gap:.3f} ({guaranteed_pct:.1f}% guaranteed)"
            )

            up_order_id = None
            down_order_id = None
            if not dry_run:
                from poly.execute import place_taker_buy
                # Buy Up
                up_result = place_taker_buy(up_token_id, up_cost)
                if up_result.success:
                    up_order_id = up_result.order_id
                    _print(f"    UP ORDER: {up_order_id}")
                else:
                    _print(f"    UP ORDER FAILED: {up_result.error}")
                    notify_error(f"Arb Up buy failed: {up_result.error}")
                    time.sleep(STOPLOSS_POLL)
                    continue

                # Buy Down
                down_result = place_taker_buy(down_token_id, down_cost)
                if down_result.success:
                    down_order_id = down_result.order_id
                    _print(f"    DOWN ORDER: {down_order_id}")
                else:
                    _print(f"    DOWN ORDER FAILED: {down_result.error}")
                    notify_error(f"Arb Down buy failed: {down_result.error}")
                    # Still have the Up position — will resolve naturally
            else:
                _print(
                    f"    [DRY RUN] Would buy Up ${up_cost:.2f} @ {up_price:.3f} "
                    f"+ Down ${down_cost:.2f} @ {down_price:.3f}"
                )

            actual_cost = up_cost + down_cost
            arb_buys.append({
                "up_price": up_price,
                "down_price": down_price,
                "up_cost": up_cost,
                "down_cost": down_cost,
                "share_pairs": share_pairs,
                "combined": combined,
                "gap": gap,
                "up_order_id": up_order_id,
                "down_order_id": down_order_id,
            })
            total_spent += actual_cost

            notify_arb_buy(
                window_time=window_time,
                up_price=up_price,
                down_price=down_price,
                up_bet=up_cost,
                down_bet=down_cost,
                guaranteed_profit_pct=guaranteed_pct,
                bankroll=state.bankroll,
            )

            # Cool down — don't stack arbs in the same second
            time.sleep(5)
        else:
            time.sleep(STOPLOSS_POLL)

    if not arb_buys:
        _print(f"  No arb opportunity (combined never < {ARB_MAX_COMBINED:.2f})")
        _wait_and_update_outcomes(state, next_ts, shutdown_event)
        return

    # Wait for resolution
    outcome = _wait_for_resolution(next_ts, shutdown_event)
    if not outcome:
        _print("  Resolution timeout!")
        state.last_window_ts = next_ts
        return

    # Calculate P&L: one side wins ($1/share), the other loses ($0)
    total_up_shares = sum(b["share_pairs"] for b in arb_buys)
    total_down_shares = total_up_shares  # equal share-pairs by design
    total_cost = sum(b["up_cost"] + b["down_cost"] for b in arb_buys)

    if outcome == "Up":
        # Up shares pay $1.00 each (minus taker fee)
        avg_up_price = sum(b["up_cost"] for b in arb_buys) / total_up_shares if total_up_shares else 0
        fee = taker_fee_rate(avg_up_price)
        payout = total_up_shares * (1 - fee)
    else:
        avg_down_price = sum(b["down_cost"] for b in arb_buys) / total_down_shares if total_down_shares else 0
        fee = taker_fee_rate(avg_down_price)
        payout = total_down_shares * (1 - fee)

    pnl = payout - total_cost
    won = pnl > 0

    # Update state
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

    state.recent_outcomes.append(outcome)
    if len(state.recent_outcomes) > 5:
        state.recent_outcomes.pop(0)
    state.last_window_ts = next_ts

    result_str = "WIN" if won else "LOSS"
    _print(
        f"  {result_str}: {outcome}  |  {len(arb_buys)} arb(s)  |  "
        f"Cost: ${total_cost:.2f} → Payout: ${payout:.2f}  |  "
        f"PnL: ${pnl:+.2f}  |  Bank: ${state.bankroll:.2f}"
    )

    notify_dip_outcome(
        window_time=window_time, outcome=outcome, num_buys=len(arb_buys),
        total_cost=total_cost, total_pnl=pnl, bankroll=state.bankroll,
        win_rate=state.win_rate, total_trades=state.total_trades, mode="ARB",
    )

    _log_trade({
        "ts": next_ts,
        "time": datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat(),
        "side": "ARB",
        "order_type": "ARB",
        "model_p": 0,
        "buy_price": 0,
        "edge": 0,
        "bet": total_cost,
        "shares_requested": round(total_up_shares, 2),
        "shares_filled": round(total_up_shares, 2),
        "fill_fraction": 1.0,
        "outcome": outcome,
        "won": won,
        "stopped_out": False,
        "pnl": pnl,
        "bankroll": state.bankroll,
        "win_rate": state.win_rate,
        "drawdown": dd,
        "arb_buys": arb_buys,
    })


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
    parser.add_argument("--dip", action="store_true", help="Dip-buy mode: buy Up when it crashes mid-window")
    parser.add_argument("--arb", action="store_true", help="Arb mode: buy both sides when combined < $1")
    args = parser.parse_args()

    # Default to maker if neither specified
    use_maker = args.maker or not args.taker

    # Determine mode
    mode = "default"
    if args.dip:
        mode = "dip"
    elif args.arb:
        mode = "arb"

    run_bot(
        bankroll=args.bankroll,
        edge_threshold=args.edge,
        max_bet_pct=args.max_bet_pct,
        kelly_mult=args.kelly,
        dry_run=not args.live,
        resume=not args.fresh,
        use_maker=use_maker,
        enable_stoploss=args.stoploss,
        mode=mode,
    )


if __name__ == "__main__":
    main()
