"""Fair-value model for 5-minute Bitcoin up/down markets.

Core strategy: Outcome mean-reversion (BUY UP after Down outcomes).

Calibrated on 1,823 resolved windows (6 days, Feb 12-18 2026):
  - P(Up | prev Down)       = 56.0%  (n=863, CI: 52.6-59.2%)  → BUY UP
  - P(Up | prev 2x Down)    = 57.6%  (n=380, CI: 52.6-62.5%)  → BUY UP (stronger)
  - P(Up | prev 3x Down)    = 59.0%  (n=161, CI: 51.3-66.3%)  → BUY UP (strongest)
  - P(Up | prev Up)          = 49.5%  (n=959)  → skip
  - P(Up | prev 2x Up)       = 47.4%  (n=475)  → skip
  - Base rate (Up):           52.6%

BUY DOWN signals do not survive 95% CI testing (CI includes 50.5%).
Position sizing: quarter-Kelly (aggressive enough to compound,
conservative enough for 0% ruin at $500+ bankroll over 863 trades).
"""

import math
from dataclasses import dataclass

from poly.btc import BTCSnapshot

# --- Conditional probabilities from 6-day analysis (1,823 windows) ---
# Graduated: deeper Down streaks → stronger signal
PROB_UP_AFTER_DOWN = 0.560  # P(Up | prev was Down), n=863
PROB_UP_AFTER_2X_DOWN = 0.576  # P(Up | prev 2 were Down), n=380
PROB_UP_AFTER_3X_DOWN = 0.590  # P(Up | prev 3+ were Down), n=161
PROB_UP_AFTER_UP = 0.495  # P(Up | prev was Up), n=959
PROB_UP_AFTER_2X_UP = 0.474  # P(Up | prev 2 were Up), n=475
PROB_UP_BASE = 0.526  # unconditional base rate

# Edge thresholds
DEFAULT_EDGE_THRESHOLD = 0.03  # 3% for pre-window
IN_PROGRESS_EDGE_THRESHOLD = 0.05  # 5% for in-progress (market has info)

# Position sizing
KELLY_MULTIPLIER = 0.25  # quarter-Kelly (0% ruin risk at $500+)
MAX_BET_PCT = 0.10  # never bet more than 10% of bankroll
MIN_BET = 5.0  # Polymarket minimum

# Polymarket fee model (5-min crypto markets)
# Official formula from docs.polymarket.com/developers/market-makers/maker-rebates-program:
#   fee = C * p * feeRate * (p * (1-p))^exponent
# where C=shares, p=price. Fee collected as shares on buys, USDC on sells.
#
# 5-min & 15-min crypto: feeRate=0.25, exponent=2, max effective=1.56% at p=0.50
# Sports (NCAAB, Serie A): feeRate=0.0175, exponent=1, max effective=0.44% at p=0.50
# Maker orders: $0 fee (+ eligible for 20% rebate pool)
FEE_RATE = 0.25  # 5-min crypto fee rate
FEE_EXPONENT = 2  # 5-min crypto exponent (squared curve)
DEFAULT_BUY_PRICE = 0.510  # typical ask price for taker orders
MAKER_BUY_PRICE = 0.500  # typical bid price for maker orders (resting on book)

# Stop-loss / cash-out thresholds
STOP_LOSS_PRICE = 0.30  # sell Up shares if price drops below 30c during window
STOP_LOSS_ENABLED = True  # enable stop-loss monitoring during live windows

# --- Dip-buy parameters (buy Up when it crashes mid-window) ---
# At low prices, taker fee is negligible: 0.16% at 22c vs 1.56% at 50c
DIP_ENTRY_PRICE = 0.35  # start buying when Up drops below 35c
DIP_LEVELS: list[tuple[float, float]] = [
    # (price_at_or_below, bankroll_fraction)
    (0.35, 0.015),  # 1.5% of bankroll when Up ≤ 35c (odds ~1.86:1)
    (0.25, 0.020),  # 2.0% of bankroll when Up ≤ 25c (odds ~3.0:1)
    (0.15, 0.025),  # 2.5% of bankroll when Up ≤ 15c (odds ~5.7:1)
]
DIP_MAX_PER_WINDOW = 0.06  # max 6% of bankroll total per window
DIP_MONITOR_SECS = 240  # monitor first 4 minutes (leave 60s before resolution)

# --- Dual-side arbitrage parameters (buy both Up+Down when combined < $1) ---
# Guaranteed profit when combined entry < $1.00: profit = $1.00 - combined_cost
ARB_MAX_COMBINED = 0.88  # buy both sides when combined price < 88c (12%+ guaranteed)
ARB_BET_PCT = 0.04  # 4% of bankroll per arb opportunity
ARB_MAX_PER_WINDOW = 0.08  # max 8% total per window
ARB_MONITOR_SECS = 240  # monitor first 4 minutes


def taker_fee_rate(price: float) -> float:
    """Polymarket taker fee as fraction of share price.

    5-min crypto: effective_rate = 0.25 * (p*(1-p))^2
    Max ~1.56% at p=0.50, drops toward 0 at extremes.

    Source: docs.polymarket.com/developers/market-makers/maker-rebates-program
    """
    return FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT


def net_odds_after_fees(buy_price: float, is_maker: bool = False) -> float:
    """Net profit per $1 bet after fees and spread.

    Winning shares pay exactly $1.00 at settlement (no settlement fee).
    Taker fee is deducted from shares received at entry.

    As taker buying at ask $0.510 with 1.56% fee:
      shares = (1/0.510) * (1 - 0.01561) = 1.9302
      net_odds = 0.9302 (profit per $1 on win)
    As maker buying at mid $0.505, no fee:
      shares = 1/0.505 = 1.9802
      net_odds = 0.9802
    """
    fee = 0.0 if is_maker else taker_fee_rate(buy_price)
    shares_per_dollar = (1.0 / buy_price) * (1 - fee)
    return shares_per_dollar - 1.0  # profit per $1 risked


@dataclass
class Signal:
    """A trading signal for a 5-minute BTC market."""

    question: str
    slug: str
    window_start: int
    minutes_until: float
    btc_price: float
    momentum_5m: float
    momentum_15m: float
    vol_1m: float
    model_prob_up: float
    market_prob_up: float
    edge: float
    side: str  # "BUY UP" or "NO EDGE"
    expected_value: float
    kelly_fraction: float
    best_bid: float
    best_ask: float
    spread: float
    liquidity: float
    tradeable: bool
    in_progress: bool = False
    seconds_remaining: float = 0.0
    prev_outcome: str = ""  # most recent resolved outcome
    # Maker-specific fields
    maker_ev: float = 0.0  # EV using maker pricing ($0 fee, bid price)
    maker_kelly: float = 0.0  # Kelly fraction for maker orders


def conditional_prob_up(prev_outcomes: list[str]) -> float:
    """Calculate P(Up) conditioned on previous window outcomes.

    Uses graduated conditional probabilities: deeper Down streaks
    produce stronger BUY UP signals (mean-reversion intensifies).

    Args:
        prev_outcomes: list of recent outcomes ["Up", "Down", ...],
                      chronological order (oldest first).

    Returns:
        Conditional probability of Up for the next window.
    """
    if not prev_outcomes:
        return PROB_UP_BASE

    last = prev_outcomes[-1]

    # Count consecutive Downs from the end
    if last == "Down":
        down_streak = 0
        for o in reversed(prev_outcomes):
            if o == "Down":
                down_streak += 1
            else:
                break
        if down_streak >= 3:
            return PROB_UP_AFTER_3X_DOWN
        elif down_streak >= 2:
            return PROB_UP_AFTER_2X_DOWN
        else:
            return PROB_UP_AFTER_DOWN

    # Count consecutive Ups from the end
    if last == "Up":
        up_streak = 0
        for o in reversed(prev_outcomes):
            if o == "Up":
                up_streak += 1
            else:
                break
        if up_streak >= 2:
            return PROB_UP_AFTER_2X_UP
        return PROB_UP_AFTER_UP

    return PROB_UP_BASE


def fair_prob_up_inprogress(
    market_price: float,
    seconds_elapsed: float,
    prev_outcomes: list[str] | None = None,
) -> float:
    """Fair P(Up) for an in-progress window.

    Markets show extreme skew during live windows (e.g. 6¢/94¢).
    We blend market price with mean-reversion base rate, trusting
    the market more as the window approaches resolution.

    market_trust goes from 30% at start to 95% near end.
    """
    total_window = 300.0  # 5 minutes
    progress = min(seconds_elapsed / total_window, 1.0)

    # Market trust increases linearly
    market_trust = 0.30 + 0.65 * progress

    # Base rate from conditional model
    base = conditional_prob_up(prev_outcomes or [])

    # Blend
    fair = market_trust * market_price + (1.0 - market_trust) * base
    return max(0.05, min(0.95, fair))


def kelly_fraction(prob: float, odds: float = 1.0, multiplier: float = KELLY_MULTIPLIER) -> float:
    """Calculate fractional Kelly bet fraction.

    f* = (p * (b + 1) - 1) / b  at quarter-Kelly (default).

    Quarter-Kelly gives 0% ruin risk at $500+ bankroll over 863 trades
    with 39.8% max drawdown (vs 66.2% at half-Kelly).
    """
    if prob <= 0.5:
        return 0.0
    f = (prob * (odds + 1) - 1) / odds
    return max(0.0, f * multiplier)


def evaluate_5m_market(
    market,  # FiveMinMarket
    snapshot: BTCSnapshot,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    prev_outcomes: list[str] | None = None,
) -> Signal:
    """Evaluate a single 5-minute market and produce a signal.

    Uses conditional mean-reversion model. BUY UP only.
    """
    is_live = market.is_in_progress
    secs_remaining = market.seconds_until_end if is_live else 0.0

    # Market price
    market_up = market.mid if market.mid > 0 else market.up_price

    # Model probability
    if is_live:
        secs_elapsed = market.seconds_elapsed
        model_up = fair_prob_up_inprogress(
            market_up, secs_elapsed, prev_outcomes
        )
        threshold = IN_PROGRESS_EDGE_THRESHOLD
    else:
        model_up = conditional_prob_up(prev_outcomes or [])
        threshold = edge_threshold

    edge = model_up - market_up

    # BUY UP only — BUY DOWN is anti-predictive (25% win rate)
    if edge > threshold:
        side = "BUY UP"
        win_prob = model_up
        buy_price = market_up
        ev = (model_up / market_up - 1) if market_up > 0 else 0
    else:
        side = "NO EDGE"
        win_prob = 0.5
        buy_price = 0.5
        ev = 0.0

    # Kelly sizing (taker)
    if buy_price > 0 and buy_price < 1 and side != "NO EDGE":
        net_odds = (1.0 - buy_price) / buy_price
        kf = kelly_fraction(win_prob, net_odds)
    else:
        kf = 0.0

    # Maker pricing: buy at bid (50c), $0 fee, better odds
    maker_ev = 0.0
    maker_kf = 0.0
    if side != "NO EDGE":
        maker_price = market.best_bid if market.best_bid > 0 else MAKER_BUY_PRICE
        maker_odds = net_odds_after_fees(maker_price, is_maker=True)
        maker_ev = (model_up / maker_price - 1) if maker_price > 0 else 0
        maker_kf = kelly_fraction(win_prob, maker_odds)

    prev_str = prev_outcomes[-1] if prev_outcomes else ""

    return Signal(
        question=market.question,
        slug=market.slug,
        window_start=market.window_start,
        minutes_until=market.minutes_until_start,
        btc_price=snapshot.price,
        momentum_5m=snapshot.momentum_5m,
        momentum_15m=snapshot.momentum_15m,
        vol_1m=snapshot.volatility_1m,
        model_prob_up=model_up,
        market_prob_up=market_up,
        edge=edge,
        side=side,
        expected_value=ev,
        kelly_fraction=kf,
        best_bid=market.best_bid,
        best_ask=market.best_ask,
        spread=market.spread,
        liquidity=market.liquidity,
        tradeable=market.is_tradeable,
        in_progress=is_live,
        seconds_remaining=secs_remaining,
        prev_outcome=prev_str,
        maker_ev=maker_ev,
        maker_kelly=maker_kf,
    )
