"""Backtesting engine for the 5-minute BTC up/down model.

Reconstructs what the model would have predicted for recently resolved
markets, compares to actual outcomes, and calculates P&L metrics.

The key challenge: we need to reconstruct the BTC momentum/volatility
snapshot as it would have looked at each historical window's start time,
not as it looks now. We do this using Kraken 1-minute candles.
"""

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests

from poly.btc import BTCSnapshot, _log_returns, _realized_vol

GAMMA_BASE = "https://gamma-api.polymarket.com"
KRAKEN_BASE = "https://api.kraken.com/0/public"

WINDOW_SECONDS = 300


@dataclass
class BacktestTrade:
    """A single backtested trade."""

    timestamp: int
    window_label: str
    actual_outcome: str  # "Up" or "Down"
    model_prob_up: float
    model_side: str  # "BUY UP", "BUY DOWN", or "NO EDGE"
    market_prob_up: float  # what market was pricing (always ~0.50)
    edge: float
    pnl: float  # dollar P&L for this trade
    won: bool
    bet_size: float = 0.0  # actual dollar bet
    bankroll_after: float = 0.0  # bankroll after this trade
    kelly_frac: float = 0.0  # Kelly fraction used


@dataclass
class BacktestResult:
    """Aggregate backtest results."""

    trades: list[BacktestTrade]
    total_windows: int
    tradeable_windows: int  # windows where model had an edge
    wins: int
    losses: int
    win_rate: float
    total_pnl: float  # total dollar P&L
    avg_edge: float
    avg_model_prob_up: float
    up_count: int
    down_count: int
    base_rate_up: float  # actual % of windows that went Up
    starting_bankroll: float = 0.0
    ending_bankroll: float = 0.0
    peak_bankroll: float = 0.0
    max_drawdown: float = 0.0  # max % drawdown from peak
    roi: float = 0.0  # total return on starting bankroll
    bankroll_history: list[float] = field(default_factory=list)


def _fetch_resolved_outcome(timestamp: int) -> str | None:
    """Fetch the resolved outcome for a 5-minute window.

    Returns "Up", "Down", or None if not resolved.
    """
    slug = f"btc-updown-5m-{timestamp}"
    try:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None

        m = results[0]
        if not m.get("closed"):
            return None

        outcome_prices = json.loads(m.get("outcomePrices", "[]"))
        if len(outcome_prices) < 2:
            return None

        # ["1","0"] = Up won, ["0","1"] = Down won
        if outcome_prices[0] == "1":
            return "Up"
        elif outcome_prices[1] == "1":
            return "Down"
        return None
    except Exception:
        return None


def _fetch_candles(hours: int = 12) -> tuple[list[list], int]:
    """Fetch OHLC candles from Kraken for backtest reconstruction.

    Kraken returns max 720 candles per request, so:
      - hours <= 11: use 1-minute candles (best granularity)
      - hours <= 58: use 5-minute candles (good for our 5-min windows)
      - hours > 58:  use 15-minute candles (coarser, up to ~7 days)

    Returns (candles, interval_minutes) where candles are sorted by time.
    """
    if hours <= 11:
        interval = 1
    elif hours <= 58:
        interval = 5
    else:
        interval = 15

    since = int(time.time()) - (hours + 1) * 3600  # +1h buffer
    resp = requests.get(
        f"{KRAKEN_BASE}/OHLC",
        params={"pair": "XBTUSD", "interval": interval, "since": since},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error") and data["error"]:
        raise RuntimeError(f"Kraken API error: {data['error']}")

    candles = data["result"]["XXBTZUSD"]
    return candles, interval


def _reconstruct_snapshot(
    candles: list[list], at_index: int, interval_min: int = 1
) -> BTCSnapshot | None:
    """Reconstruct a BTCSnapshot using candles up to the given index.

    Uses candles[0..at_index] to compute momentum and volatility
    as they would have looked at that point in time.

    interval_min: candle interval in minutes (1, 5, or 15).
    Momentum lookbacks are adjusted to cover the same real-time spans:
      - 5-min momentum: 5/interval candles back
      - 15-min momentum: 15/interval candles back
    """
    min_candles = max(5, 20 // interval_min)
    if at_index < min_candles:
        return None

    closes = [float(c[4]) for c in candles[: at_index + 1]]
    price = closes[-1]

    # How many candles cover 5 minutes and 15 minutes?
    candles_5m = max(1, 5 // interval_min)
    candles_15m = max(1, 15 // interval_min)

    # 5-minute momentum
    momentum_5m = 0.0
    if len(closes) > candles_5m:
        momentum_5m = math.log(closes[-1] / closes[-(candles_5m + 1)])

    # 15-minute momentum
    momentum_15m = 0.0
    if len(closes) > candles_15m:
        momentum_15m = math.log(closes[-1] / closes[-(candles_15m + 1)])

    # Realized vol from available candles
    # Scale annualization factor by interval size
    periods_per_year = 525_960 / interval_min
    lookback = min(60, len(closes))
    recent = closes[-lookback:]
    log_rets = _log_returns(recent)
    vol = _realized_vol(log_rets, periods_per_year)

    return BTCSnapshot(
        price=price,
        chainlink_price=None,  # not available for historical reconstruction
        momentum_5m=momentum_5m,
        momentum_15m=momentum_15m,
        volatility_1m=vol,
        volatility_1h=vol,
        recent_closes=recent,
    )


def run_backtest(
    hours: int = 6,
    edge_threshold: float = 0.03,
    bankroll: float = 100.0,
    max_bet: float = 0.0,  # 0 = auto (10% of bankroll)
    workers: int = 20,
) -> BacktestResult:
    """Run a backtest over the last N hours of resolved 5-minute markets.

    For each resolved window:
    1. Reconstruct BTCSnapshot from 1-min candles at that time
    2. Compute model P(Up)
    3. Compare to actual outcome
    4. Size bet via Kelly criterion on current bankroll
    5. Track bankroll evolution
    """
    from poly.model import fair_prob_up, kelly_fraction as calc_kelly

    starting_bankroll = bankroll
    current_bankroll = bankroll
    peak_bankroll = bankroll
    max_drawdown = 0.0
    bankroll_history = [bankroll]

    print(f"  Fetching {hours}h of candle data...")
    candles, interval_min = _fetch_candles(hours)
    coverage_h = len(candles) * interval_min / 60
    print(f"  Got {len(candles)} candles ({interval_min}-min interval, {coverage_h:.1f}h coverage)")

    # Build a time→index map for candles
    candle_times = {int(c[0]): i for i, c in enumerate(candles)}

    # Generate timestamps for all 5-min windows in the backtest period
    now = int(time.time())
    start = now - hours * 3600
    start_aligned = (start // WINDOW_SECONDS) * WINDOW_SECONDS

    timestamps = []
    t = start_aligned
    while t < now - WINDOW_SECONDS:  # exclude current/running window
        timestamps.append(t)
        t += WINDOW_SECONDS

    print(f"  Checking {len(timestamps)} windows for resolved outcomes...")

    # Fetch outcomes in parallel (batched for large backtests)
    outcomes = {}
    batch_size = 100
    for batch_start in range(0, len(timestamps), batch_size):
        batch = timestamps[batch_start : batch_start + batch_size]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_resolved_outcome, ts): ts for ts in batch
            }
            for future in as_completed(futures):
                ts = futures[future]
                result = future.result()
                if result:
                    outcomes[ts] = result
        if batch_start + batch_size < len(timestamps):
            fetched = min(batch_start + batch_size, len(timestamps))
            print(f"    ...fetched {fetched}/{len(timestamps)} ({len(outcomes)} resolved so far)")

    print(f"  Got {len(outcomes)} resolved outcomes")

    # For each resolved window, reconstruct snapshot and evaluate
    trades = []
    for ts in sorted(outcomes.keys()):
        # Find the most recent candle that CLOSED BEFORE this window starts.
        # A candle starting at candle_time closes at candle_time + interval*60.
        # We can only use candles whose close is available at decision time (ts).
        best_idx = None
        best_diff = float("inf")
        for candle_time, idx in candle_times.items():
            candle_close_time = candle_time + interval_min * 60
            if candle_close_time <= ts:
                diff = ts - candle_close_time
                if diff < best_diff:
                    best_diff = diff
                    best_idx = idx

        max_gap = interval_min * 60 + 60  # allow up to 1 interval + 60s gap
        if best_idx is None or best_diff > max_gap:
            continue

        snapshot = _reconstruct_snapshot(candles, best_idx, interval_min)
        if snapshot is None:
            continue

        model_up = fair_prob_up(snapshot, minutes_ahead=0)
        actual = outcomes[ts]

        # Assume market was pricing at ~0.505 (observed default)
        market_up = 0.505
        edge = model_up - market_up

        # ONLY BUY UP — backtest proved BUY DOWN is anti-predictive (25% win rate)
        if edge > edge_threshold:
            side = "BUY UP"
            win_prob = model_up
            buy_price = market_up
            won = actual == "Up"
        else:
            side = "NO EDGE"
            win_prob = 0.5
            buy_price = 0.5
            won = False

        # Kelly sizing on current bankroll
        bet_size = 0.0
        kf = 0.0
        pnl = 0.0
        if side != "NO EDGE" and current_bankroll > 5.0:
            net_odds = (1.0 - buy_price) / buy_price if buy_price > 0 else 1.0
            kf = calc_kelly(win_prob, net_odds)
            cap = max_bet if max_bet > 0 else current_bankroll * 0.10
            bet_size = min(current_bankroll * kf, cap, current_bankroll)
            bet_size = max(bet_size, 0.0)

            # Minimum bet check
            if bet_size < 5.0:
                bet_size = 0.0
                side = "NO EDGE"  # too small to trade

        if bet_size > 0:
            if won:
                pnl = bet_size * (1.0 - buy_price) / buy_price  # net winnings
            else:
                pnl = -bet_size  # lose the bet

            current_bankroll += pnl
            if current_bankroll > peak_bankroll:
                peak_bankroll = current_bankroll
            drawdown = (peak_bankroll - current_bankroll) / peak_bankroll if peak_bankroll > 0 else 0
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        bankroll_history.append(current_bankroll)

        trades.append(
            BacktestTrade(
                timestamp=ts,
                window_label=f"{ts}",
                actual_outcome=actual,
                model_prob_up=model_up,
                model_side=side,
                market_prob_up=market_up,
                edge=edge,
                pnl=pnl,
                won=won,
                bet_size=bet_size,
                bankroll_after=current_bankroll,
                kelly_frac=kf,
            )
        )

    # Aggregate results
    actionable = [t for t in trades if t.model_side != "NO EDGE"]
    wins = sum(1 for t in actionable if t.won)
    losses = len(actionable) - wins
    total_pnl = current_bankroll - starting_bankroll
    up_count = sum(1 for t in trades if t.actual_outcome == "Up")
    down_count = sum(1 for t in trades if t.actual_outcome == "Down")

    return BacktestResult(
        trades=trades,
        total_windows=len(trades),
        tradeable_windows=len(actionable),
        wins=wins,
        losses=losses,
        win_rate=wins / len(actionable) if actionable else 0,
        total_pnl=total_pnl,
        avg_edge=sum(abs(t.edge) for t in actionable) / len(actionable) if actionable else 0,
        avg_model_prob_up=sum(t.model_prob_up for t in trades) / len(trades) if trades else 0.5,
        up_count=up_count,
        down_count=down_count,
        base_rate_up=up_count / len(trades) if trades else 0.5,
        starting_bankroll=starting_bankroll,
        ending_bankroll=current_bankroll,
        peak_bankroll=peak_bankroll,
        max_drawdown=max_drawdown,
        roi=(current_bankroll - starting_bankroll) / starting_bankroll if starting_bankroll > 0 else 0,
        bankroll_history=bankroll_history,
    )


def print_backtest(result: BacktestResult) -> None:
    """Print backtest results in a readable format."""
    from datetime import datetime, timezone

    print("\n" + "=" * 72)
    print("  BACKTEST RESULTS")
    print("=" * 72)

    print(f"\n  Total windows analyzed:  {result.total_windows}")
    print(f"  Actual outcomes:         {result.up_count} Up / {result.down_count} Down")
    print(f"  Base rate (Up):          {result.base_rate_up:.1%}")

    print(f"\n  Actionable signals:      {result.tradeable_windows}")
    if result.tradeable_windows > 0:
        print(f"  Wins / Losses:           {result.wins} / {result.losses}")
        print(f"  Win rate:                {result.win_rate:.1%}")
        print(f"  Avg absolute edge:       {result.avg_edge:.1%}")

    # Bankroll section
    print(f"\n  --- BANKROLL ---")
    print(f"  Starting:                ${result.starting_bankroll:,.2f}")
    print(f"  Ending:                  ${result.ending_bankroll:,.2f}")
    print(f"  P&L:                     {'+' if result.total_pnl >= 0 else ''}${result.total_pnl:,.2f}")
    print(f"  ROI:                     {result.roi:+.1%}")
    print(f"  Peak:                    ${result.peak_bankroll:,.2f}")
    print(f"  Max drawdown:            {result.max_drawdown:.1%}")
    if result.tradeable_windows > 0:
        print(f"  Avg bet size:            ${sum(t.bet_size for t in result.trades if t.bet_size > 0) / max(1, result.tradeable_windows):,.2f}")
        print(f"  P&L per trade:           {'+' if result.total_pnl >= 0 else ''}${result.total_pnl / result.tradeable_windows:,.2f}")

    # Bankroll curve (ASCII sparkline)
    if len(result.bankroll_history) > 2:
        _print_bankroll_chart(result.bankroll_history, result.starting_bankroll)

    # Show trade breakdown by side
    actionable = [t for t in result.trades if t.model_side != "NO EDGE"]
    if actionable:
        up_trades = [t for t in actionable if t.model_side == "BUY UP"]
        down_trades = [t for t in actionable if t.model_side == "BUY DOWN"]
        up_wins = sum(1 for t in up_trades if t.won)
        down_wins = sum(1 for t in down_trades if t.won)
        up_pnl = sum(t.pnl for t in up_trades)
        down_pnl = sum(t.pnl for t in down_trades)

        print(f"\n  --- BY SIDE ---")
        if up_trades:
            print(f"  BUY UP:   {up_wins}/{len(up_trades)} wins ({up_wins/len(up_trades):.0%})  P&L: {'+' if up_pnl >= 0 else ''}${up_pnl:,.2f}")
        if down_trades:
            print(f"  BUY DOWN: {down_wins}/{len(down_trades)} wins ({down_wins/len(down_trades):.0%})  P&L: {'+' if down_pnl >= 0 else ''}${down_pnl:,.2f}")

    # Show recent trades
    if actionable:
        show_n = min(30, len(actionable))
        print(f"\n  --- RECENT TRADES (last {show_n}) ---")
        print(f"  {'Time UTC':>17}  {'Side':>10}  {'Model':>6}  {'Actual':>6}  {'Bet':>8}  {'PnL':>9}  {'Bank':>9}  {'Result':>6}")
        print(f"  {'-'*17}  {'-'*10}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*9}  {'-'*9}  {'-'*6}")
        for t in actionable[-show_n:]:
            result_str = "WIN" if t.won else "LOSS"
            dt = datetime.fromtimestamp(t.timestamp, tz=timezone.utc)
            time_str = dt.strftime("%m-%d %H:%M")
            print(
                f"  {time_str:>17}  {t.model_side:>10}  "
                f"{t.model_prob_up:>5.1%}  {t.actual_outcome:>6}  "
                f"${t.bet_size:>7.2f}  "
                f"{'+'if t.pnl>=0 else ''}${t.pnl:>7.2f}  "
                f"${t.bankroll_after:>8.2f}  {result_str:>6}"
            )

    print()


def _print_bankroll_chart(history: list[float], starting: float) -> None:
    """Print a simple ASCII chart of bankroll over time."""
    width = 60
    height = 12

    # Sample the history to fit the width
    if len(history) > width:
        step = len(history) / width
        sampled = [history[int(i * step)] for i in range(width)]
    else:
        sampled = history

    mn = min(sampled)
    mx = max(sampled)
    rng = mx - mn if mx > mn else 1

    print(f"\n  --- BANKROLL CURVE ---")
    print(f"  ${mx:>8.2f} |", end="")

    # Build the chart
    rows = []
    for row in range(height):
        threshold = mx - (row / (height - 1)) * rng
        line = ""
        for val in sampled:
            if val >= threshold:
                line += "#"
            else:
                line += " "
        rows.append(line)

    for i, row in enumerate(rows):
        if i == 0:
            print(row)
        elif i == height - 1:
            print(f"  ${mn:>8.2f} |{row}")
        else:
            print(f"            |{row}")

    # X axis
    print(f"            +{'-' * len(sampled)}")
    print(f"             {'start':<{len(sampled)//2}}{'now':>{len(sampled)//2}}")
