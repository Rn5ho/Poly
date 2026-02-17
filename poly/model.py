"""Fair-value pricing model for Bitcoin binary outcome markets.

Uses a log-normal model (equivalent to digital/binary option pricing)
to calculate the probability that BTC will be above a strike price K
at expiry time T, given current price S and annualized volatility σ.

    P(S_T > K) = Φ(d₂)

    where d₂ = [ln(S/K) - (σ²/2)·τ] / (σ·√τ)

For short-duration markets (hours to days), drift is negligible so we
set μ = 0. The edge is the difference between our model probability
and the market's implied probability.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from scipy.stats import norm


@dataclass
class Signal:
    """A trading signal for a Polymarket BTC market."""

    question: str
    slug: str
    strike: float
    expiry: datetime
    hours_left: float
    btc_price: float
    volatility: float  # annualized
    model_prob: float  # our fair probability of YES
    market_prob: float  # Polymarket's implied probability of YES
    edge: float  # model_prob - market_prob
    side: str  # "BUY YES", "BUY NO", or "NO EDGE"
    expected_value: float  # expected profit per $1 risked
    best_bid: float
    best_ask: float
    volume_24h: float
    liquidity: float


def fair_probability(spot: float, strike: float, vol: float, tau: float) -> float:
    """Calculate the fair probability that BTC > strike at expiry.

    Args:
        spot: Current BTC price.
        strike: Strike price for the market.
        vol: Annualized volatility (e.g. 0.65 for 65%).
        tau: Time to expiry in years.

    Returns:
        Probability between 0 and 1.
    """
    if tau <= 0:
        # Already expired: deterministic
        return 1.0 if spot > strike else 0.0

    if vol <= 0:
        return 1.0 if spot > strike else 0.0

    d2 = (math.log(spot / strike) - 0.5 * vol**2 * tau) / (vol * math.sqrt(tau))
    return float(norm.cdf(d2))


def time_to_expiry_years(expiry: datetime) -> float:
    """Calculate time to expiry in years from now."""
    now = datetime.now(timezone.utc)
    delta = expiry - now
    seconds = max(delta.total_seconds(), 0)
    return seconds / (365.25 * 24 * 3600)


def evaluate_market(
    question: str,
    slug: str,
    strike: float,
    expiry: datetime,
    market_yes_price: float,
    best_bid: float,
    best_ask: float,
    volume_24h: float,
    liquidity: float,
    btc_price: float,
    volatility: float,
    edge_threshold: float = 0.03,
) -> Signal:
    """Evaluate a single market and produce a trading signal.

    Args:
        edge_threshold: Minimum edge (as probability) to generate a signal.
                        Default 3% — accounts for spread, fees, and noise.
    """
    tau = time_to_expiry_years(expiry)
    hours_left = tau * 365.25 * 24

    model_prob = fair_probability(btc_price, strike, volatility, tau)

    # Market's implied probability from the YES token price
    market_prob = market_yes_price
    edge = model_prob - market_prob

    # Determine side
    if edge > edge_threshold:
        side = "BUY YES"
        # Buying YES at market_prob, expected value = model_prob / market_prob - 1
        ev = (model_prob / market_prob - 1) if market_prob > 0 else 0
    elif edge < -edge_threshold:
        side = "BUY NO"
        # Buying NO at (1 - market_prob), expected value
        no_model = 1 - model_prob
        no_market = 1 - market_prob
        ev = (no_model / no_market - 1) if no_market > 0 else 0
    else:
        side = "NO EDGE"
        ev = 0.0

    return Signal(
        question=question,
        slug=slug,
        strike=strike,
        expiry=expiry,
        hours_left=hours_left,
        btc_price=btc_price,
        volatility=volatility,
        model_prob=model_prob,
        market_prob=market_prob,
        edge=edge,
        side=side,
        expected_value=ev,
        best_bid=best_bid,
        best_ask=best_ask,
        volume_24h=volume_24h,
        liquidity=liquidity,
    )
