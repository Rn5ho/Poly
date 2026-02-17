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
from dataclasses import dataclass

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
    pnl: float  # +1 if we'd have won, -1 if lost (unit bet)
    won: bool


@dataclass
class BacktestResult:
    """Aggregate backtest results."""

    trades: list[BacktestTrade]
    total_windows: int
    tradeable_windows: int  # windows where model had an edge
    wins: int
    losses: int
    win_rate: float
    total_pnl: float  # sum of edge-weighted P&L
    avg_edge: float
    avg_model_prob_up: float
    up_count: int
    down_count: int
    base_rate_up: float  # actual % of windows that went Up


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


def _fetch_minute_candles(hours: int = 12) -> list[list]:
    """Fetch 1-minute candles from Kraken for backtest reconstruction.

    Returns list of [time, open, high, low, close, vwap, volume, count].
    """
    since = int(time.time()) - hours * 3600
    resp = requests.get(
        f"{KRAKEN_BASE}/OHLC",
        params={"pair": "XBTUSD", "interval": 1, "since": since},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")
    return data["result"]["XXBTZUSD"]


def _reconstruct_snapshot(candles: list[list], at_index: int) -> BTCSnapshot | None:
    """Reconstruct a BTCSnapshot using candles up to the given index.

    Uses candles[0..at_index] to compute momentum and volatility
    as they would have looked at that point in time.
    """
    if at_index < 20:  # need at least 20 candles for meaningful signals
        return None

    closes = [float(c[4]) for c in candles[: at_index + 1]]
    price = closes[-1]

    # 5-minute momentum (last 5 candles)
    momentum_5m = 0.0
    if len(closes) >= 6:
        momentum_5m = math.log(closes[-1] / closes[-6])

    # 15-minute momentum (last 15 candles)
    momentum_15m = 0.0
    if len(closes) >= 16:
        momentum_15m = math.log(closes[-1] / closes[-16])

    # 1-minute realized vol from last 60 candles (or whatever's available)
    lookback = min(60, len(closes))
    recent = closes[-lookback:]
    log_rets = _log_returns(recent)
    vol_1m = _realized_vol(log_rets, 525_960)

    return BTCSnapshot(
        price=price,
        momentum_5m=momentum_5m,
        momentum_15m=momentum_15m,
        volatility_1m=vol_1m,
        volatility_1h=vol_1m,  # approximate; hourly not available per-minute
        recent_closes=recent,
    )


def run_backtest(
    hours: int = 6,
    edge_threshold: float = 0.03,
    workers: int = 20,
) -> BacktestResult:
    """Run a backtest over the last N hours of resolved 5-minute markets.

    For each resolved window:
    1. Reconstruct BTCSnapshot from 1-min candles at that time
    2. Compute model P(Up)
    3. Compare to actual outcome
    4. Calculate hypothetical P&L
    """
    from poly.model import fair_prob_up

    print(f"  Fetching {hours}h of 1-minute candles...")
    candles = _fetch_minute_candles(hours + 1)  # +1h buffer for lookback
    print(f"  Got {len(candles)} candles")

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

    print(f"  Checking {len(timestamps)} windows...")

    # Fetch outcomes in parallel
    outcomes = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_resolved_outcome, ts): ts for ts in timestamps
        }
        for future in as_completed(futures):
            ts = futures[future]
            result = future.result()
            if result:
                outcomes[ts] = result

    print(f"  Got {len(outcomes)} resolved outcomes")

    # For each resolved window, reconstruct snapshot and evaluate
    trades = []
    for ts in sorted(outcomes.keys()):
        # Find the candle index closest to this window's start time
        # Candle at window start = what we'd see when deciding to trade
        best_idx = None
        best_diff = float("inf")
        for candle_time, idx in candle_times.items():
            diff = abs(candle_time - ts)
            if diff < best_diff and candle_time <= ts:
                best_diff = diff
                best_idx = idx

        if best_idx is None or best_diff > 120:  # skip if >2min gap
            continue

        snapshot = _reconstruct_snapshot(candles, best_idx)
        if snapshot is None:
            continue

        model_up = fair_prob_up(snapshot, minutes_ahead=0)
        actual = outcomes[ts]

        # Assume market was pricing at ~0.505 (observed default)
        market_up = 0.505
        edge = model_up - market_up

        if edge > edge_threshold:
            side = "BUY UP"
            won = actual == "Up"
            # P&L: bet at market price, collect 1.0 if win
            pnl = (1.0 - market_up) if won else -market_up
        elif edge < -edge_threshold:
            side = "BUY DOWN"
            won = actual == "Down"
            market_down = 1 - market_up
            pnl = (1.0 - market_down) if won else -market_down
        else:
            side = "NO EDGE"
            won = False
            pnl = 0.0

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
            )
        )

    # Aggregate results
    actionable = [t for t in trades if t.model_side != "NO EDGE"]
    wins = sum(1 for t in actionable if t.won)
    losses = len(actionable) - wins
    total_pnl = sum(t.pnl for t in trades)
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
    )


def print_backtest(result: BacktestResult) -> None:
    """Print backtest results in a readable format."""
    print("=" * 72)
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
        print(f"  Total P&L (unit bets):   {result.total_pnl:+.2f}")
        print(f"  P&L per trade:           {result.total_pnl / result.tradeable_windows:+.3f}")
    else:
        print("  (no trades taken)")

    # Show recent trades
    actionable = [t for t in result.trades if t.model_side != "NO EDGE"]
    if actionable:
        print(f"\n  Recent trades (last 20):")
        print(f"  {'Time':>12}  {'Side':>10}  {'Model':>6}  {'Actual':>6}  {'PnL':>7}  {'Result':>6}")
        print(f"  {'-'*12}  {'-'*10}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*6}")
        for t in actionable[-20:]:
            result_str = "WIN" if t.won else "LOSS"
            print(
                f"  {t.timestamp:>12}  {t.model_side:>10}  "
                f"{t.model_prob_up:>5.1%}  {t.actual_outcome:>6}  "
                f"{t.pnl:>+6.3f}  {result_str:>6}"
            )

    print()
