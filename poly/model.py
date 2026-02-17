"""Fair-value model for 5-minute Bitcoin up/down markets.

For a 5-minute window, the question is simply: will BTC go up or stay
flat (>= start price) vs. go down (< start price)?

Model components:
  1. Base rate: ~50.0% for Up (>= rule gives tiny edge to Up, but
     effectively 50/50 at BTC's tick size)
  2. Momentum signal: short-term autocorrelation in 1-minute returns.
     If BTC has been trending over the last 5-15 minutes, there's a
     measurable continuation probability.
  3. Momentum decay: the signal only predicts the *next* 5-min window.
     For windows further in the future, momentum fades back to 50/50
     with a half-life of ~10 minutes.

The edge is: model_prob - market_prob. When the market deviates from
our fair value, that's the opportunity.
"""

import math
from dataclasses import dataclass

from scipy.stats import norm

from poly.btc import BTCSnapshot


# How much weight to give the raw momentum signal
MOMENTUM_WEIGHT = 0.4

# Half-life for momentum decay (minutes). Momentum signal halves every
# this many minutes into the future. At 10 min, a window 10 min away
# gets 50% of the signal; at 20 min, 25%; at 30 min, 12.5%.
MOMENTUM_HALFLIFE_MIN = 10.0

# Minimum edge to flag as actionable (must overcome spread + fees)
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
    edge: float  # model - market (positive = Up underpriced)
    side: str  # "BUY UP", "BUY DOWN", or "NO EDGE"
    expected_value: float
    best_bid: float
    best_ask: float
    spread: float
    liquidity: float
    tradeable: bool


def fair_prob_up(snapshot: BTCSnapshot, minutes_ahead: float = 0.0) -> float:
    """Calculate fair probability of BTC going up in a 5-minute window.

    Args:
        snapshot: Current BTC price/momentum/vol data.
        minutes_ahead: How many minutes until this window starts.
            0 = current window (full momentum signal).
            Higher values decay the momentum toward 50/50.
    """
    vol_annual = snapshot.volatility_1m
    if vol_annual <= 0:
        vol_annual = snapshot.volatility_1h
    if vol_annual <= 0:
        return 0.50

    # 5-minute volatility
    tau = 5.0 / 525_960  # 5 minutes as fraction of year
    sigma_5m = vol_annual * math.sqrt(tau)
    if sigma_5m <= 0:
        return 0.50

    # Raw momentum signal (weighted combo of 5m and 15m)
    raw_mom = (
        MOMENTUM_WEIGHT * snapshot.momentum_5m
        + (MOMENTUM_WEIGHT * 0.5) * snapshot.momentum_15m
    )

    # Decay: momentum predicts the next window, not ones far in the future.
    # Exponential decay with configurable half-life.
    decay = 0.5 ** (minutes_ahead / MOMENTUM_HALFLIFE_MIN)
    mom_signal = raw_mom * decay

    # P(Up) = Φ(momentum_mean / sigma_5m)
    prob = float(norm.cdf(mom_signal / sigma_5m))

    # Clamp to reasonable range
    return max(0.30, min(0.70, prob))


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
        ev = (model_up / market_up - 1) if market_up > 0 else 0
    elif edge < -edge_threshold:
        side = "BUY DOWN"
        model_down = 1 - model_up
        market_down = 1 - market_up
        ev = (model_down / market_down - 1) if market_down > 0 else 0
    else:
        side = "NO EDGE"
        ev = 0.0

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
        best_bid=market.best_bid,
        best_ask=market.best_ask,
        spread=market.spread,
        liquidity=market.liquidity,
        tradeable=market.is_tradeable,
    )
