"""Fair-value model for 5-minute Bitcoin up/down markets.

Backtesting revealed:
  - Strong Up momentum (>65%): 70% win rate — highly predictive
  - Lean Down momentum (35-45%): 60% win rate — solid
  - Strong Down momentum (<35%): ANTI-predictive (mean-reversion)
  - Base rate is ~53.5% Up (from the >= resolution rule)

Model adjustments:
  1. Base rate bias: +1.5% toward Up (>= rule + empirical 53.5%)
  2. Asymmetric momentum: Up momentum gets full weight; Down momentum
     gets reduced weight (mean-reversion dampens extreme Down signals)
  3. Wider clamp range: 25-75% (was 30-70%)
  4. Momentum decay for future windows (half-life 10 min)
"""

import math
from dataclasses import dataclass

from scipy.stats import norm

from poly.btc import BTCSnapshot


# Momentum weights (asymmetric: Up > Down based on backtest)
MOMENTUM_WEIGHT_UP = 0.45  # weight when momentum is positive
MOMENTUM_WEIGHT_DOWN = 0.25  # weight when momentum is negative (mean-reversion dampens)

# 15-minute momentum contributes at 50% of the primary weight
SECONDARY_WEIGHT_RATIO = 0.5

# Base rate adjustment: >= rule + empirical bias
BASE_PROB_UP = 0.515

# Momentum decay half-life (minutes into the future)
MOMENTUM_HALFLIFE_MIN = 10.0

# Probability clamp range
PROB_FLOOR = 0.25
PROB_CEIL = 0.75

# Minimum edge to flag as actionable
DEFAULT_EDGE_THRESHOLD = 0.03


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
    side: str  # "BUY UP", "BUY DOWN", or "NO EDGE"
    expected_value: float
    kelly_fraction: float  # optimal bet size (fraction of bankroll)
    best_bid: float
    best_ask: float
    spread: float
    liquidity: float
    tradeable: bool


def fair_prob_up(snapshot: BTCSnapshot, minutes_ahead: float = 0.0) -> float:
    """Calculate fair probability of BTC going up in a 5-minute window.

    Uses asymmetric momentum weighting: Up momentum is trusted more
    than Down momentum (backtest shows Down extremes mean-revert).
    """
    vol_annual = snapshot.volatility_1m
    if vol_annual <= 0:
        vol_annual = snapshot.volatility_1h
    if vol_annual <= 0:
        return BASE_PROB_UP

    # 5-minute volatility
    tau = 5.0 / 525_960
    sigma_5m = vol_annual * math.sqrt(tau)
    if sigma_5m <= 0:
        return BASE_PROB_UP

    # Asymmetric momentum: choose weight based on direction
    mom_5m = snapshot.momentum_5m
    mom_15m = snapshot.momentum_15m

    # Primary weight depends on momentum direction
    if mom_5m >= 0:
        w_primary = MOMENTUM_WEIGHT_UP
    else:
        w_primary = MOMENTUM_WEIGHT_DOWN

    # Combine 5m and 15m signals
    raw_mom = w_primary * mom_5m + (w_primary * SECONDARY_WEIGHT_RATIO) * mom_15m

    # Decay for future windows
    decay = 0.5 ** (minutes_ahead / MOMENTUM_HALFLIFE_MIN)
    mom_signal = raw_mom * decay

    # Shift the base probability by the momentum signal
    # z = (base_shift + momentum) / sigma
    # where base_shift encodes the base rate advantage for Up
    base_shift = norm.ppf(BASE_PROB_UP) * sigma_5m  # shift that gives BASE_PROB_UP
    z = (base_shift + mom_signal) / sigma_5m
    prob = float(norm.cdf(z))

    return max(PROB_FLOOR, min(PROB_CEIL, prob))


def kelly_fraction(prob: float, odds: float = 1.0) -> float:
    """Calculate Kelly criterion bet fraction.

    For a bet at even odds (buy at ~0.50):
      f* = (p * (b + 1) - 1) / b
      where p = win probability, b = net odds (payout / stake)

    For Polymarket at price ~0.50: b = (1.0 - price) / price ≈ 1.0
    So: f* = 2*p - 1

    We use half-Kelly for safety (halve the fraction).
    """
    if prob <= 0.5:
        return 0.0
    f = (prob * (odds + 1) - 1) / odds
    return max(0.0, f * 0.5)  # half-Kelly


def evaluate_5m_market(
    market,  # FiveMinMarket
    snapshot: BTCSnapshot,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
) -> Signal:
    """Evaluate a single 5-minute market and produce a signal."""
    minutes_ahead = market.minutes_until_start
    model_up = fair_prob_up(snapshot, minutes_ahead)
    market_up = market.mid if market.mid > 0 else market.up_price

    edge = model_up - market_up

    if edge > edge_threshold:
        side = "BUY UP"
        win_prob = model_up
        buy_price = market_up
        ev = (model_up / market_up - 1) if market_up > 0 else 0
    elif edge < -edge_threshold:
        side = "BUY DOWN"
        win_prob = 1 - model_up
        buy_price = 1 - market_up
        ev = ((1 - model_up) / (1 - market_up) - 1) if market_up < 1 else 0
    else:
        side = "NO EDGE"
        win_prob = 0.5
        buy_price = 0.5
        ev = 0.0

    # Kelly sizing: net odds = (1 - buy_price) / buy_price
    if buy_price > 0 and buy_price < 1 and side != "NO EDGE":
        net_odds = (1.0 - buy_price) / buy_price
        kf = kelly_fraction(win_prob, net_odds)
    else:
        kf = 0.0

    return Signal(
        question=market.question,
        slug=market.slug,
        window_start=market.window_start,
        minutes_until=minutes_ahead,
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
    )
