"""Backtesting engine for the 5-minute BTC up/down conditional model.

Uses the outcome mean-reversion strategy: P(Up) depends on previous
window outcomes, not momentum. Validates against resolved markets.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from poly.polymarket import _fetch_resolved_outcome

WINDOW_SECONDS = 300


@dataclass
class BacktestTrade:
    """A single backtested trade."""

    timestamp: int
    window_label: str
    actual_outcome: str  # "Up" or "Down"
    model_prob_up: float
    model_side: str  # "BUY UP" or "NO EDGE"
    market_prob_up: float  # what market was pricing (always ~0.50)
    edge: float
    pnl: float  # dollar P&L for this trade
    won: bool
    bet_size: float = 0.0
    bankroll_after: float = 0.0
    kelly_frac: float = 0.0


@dataclass
class BacktestResult:
    """Aggregate backtest results."""

    trades: list[BacktestTrade]
    total_windows: int
    tradeable_windows: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_edge: float
    avg_model_prob_up: float
    up_count: int
    down_count: int
    base_rate_up: float
    starting_bankroll: float = 0.0
    ending_bankroll: float = 0.0
    peak_bankroll: float = 0.0
    max_drawdown: float = 0.0
    roi: float = 0.0
    bankroll_history: list[float] = field(default_factory=list)


def run_backtest(
    hours: int = 6,
    edge_threshold: float = 0.03,
    bankroll: float = 100.0,
    max_bet: float = 0.0,  # 0 = auto (10% of bankroll)
    workers: int = 20,
    as_maker: bool = False,
) -> BacktestResult:
    """Run a backtest over the last N hours of resolved 5-minute markets.

    For each resolved window:
    1. Compute conditional P(Up) based on previous outcomes
    2. Compare to assumed market price (~0.505)
    3. Size bet via Kelly criterion on current bankroll
    4. Track bankroll evolution
    """
    from poly.model import conditional_prob_up, kelly_fraction as calc_kelly, net_odds_after_fees, DEFAULT_BUY_PRICE, MAKER_BUY_PRICE

    starting_bankroll = bankroll
    current_bankroll = bankroll
    peak_bankroll = bankroll
    max_drawdown = 0.0
    bankroll_history = [bankroll]

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

    # Build ordered sequence of resolved outcomes for conditional model
    sorted_ts = sorted(outcomes.keys())
    recent_outcomes: list[str] = []  # rolling window of recent outcomes

    trades = []
    for i, ts in enumerate(sorted_ts):
        actual = outcomes[ts]

        # Check if this window is consecutive with the previous
        is_consecutive = (
            i > 0 and ts - sorted_ts[i - 1] == WINDOW_SECONDS
        )
        if not is_consecutive:
            recent_outcomes = []  # reset on gap

        # Conditional model: P(Up) based on previous outcomes
        model_up = conditional_prob_up(recent_outcomes)

        # Market pricing: taker buys at ask (~0.510), maker at bid (~0.500)
        buy_price = MAKER_BUY_PRICE if as_maker else DEFAULT_BUY_PRICE
        market_up = buy_price
        edge = model_up - market_up

        # BUY UP only when we have positive edge
        if edge > edge_threshold:
            side = "BUY UP"
            win_prob = model_up
            won = actual == "Up"
        else:
            side = "NO EDGE"
            win_prob = 0.5
            buy_price = 0.5
            won = False

        # Update outcome history AFTER using it for prediction
        recent_outcomes.append(actual)
        if len(recent_outcomes) > 5:
            recent_outcomes.pop(0)

        # Kelly sizing on current bankroll (fee-aware odds)
        bet_size = 0.0
        kf = 0.0
        pnl = 0.0
        if side != "NO EDGE" and current_bankroll > 5.0:
            net_odds = net_odds_after_fees(buy_price, is_maker=as_maker)
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
                pnl = bet_size * net_odds  # net winnings after fees
            else:
                pnl = -bet_size  # lose the full bet

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

    # Show trade breakdown
    actionable = [t for t in result.trades if t.model_side != "NO EDGE"]
    if actionable:
        up_trades = [t for t in actionable if t.model_side == "BUY UP"]
        up_wins = sum(1 for t in up_trades if t.won)
        up_pnl = sum(t.pnl for t in up_trades)
        print(f"\n  --- BY SIDE ---")
        if up_trades:
            print(f"  BUY UP:   {up_wins}/{len(up_trades)} wins ({up_wins/len(up_trades):.0%})  P&L: {'+' if up_pnl >= 0 else ''}${up_pnl:,.2f}")

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
