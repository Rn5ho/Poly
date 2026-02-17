"""Fair-value model for 5-minute Bitcoin up/down markets.

Two strategies:

1. PRE-WINDOW (market ~50/50, window hasn't started):
   - Weak signal from momentum, main edge is >= rule base rate
   - Conservative sizing

2. IN-PROGRESS (market has swung, window is live):
   - Market overreacts to early price movement
   - Use Chainlink oracle price vs window start price to gauge true direction
   - Mean-reversion at micro timescales creates edge on extreme pricing
   - Bigger opportunity but higher risk

The in-progress strategy is the primary edge. BTC 5-min returns are
near-random at pre-window stage, but markets OVERREACT to early movement
in live windows, creating mispricing.
"""

import math
import time
from dataclasses import dataclass

from scipy.stats import norm

from poly.btc import BTCSnapshot


# --- Base rate ---
BASE_PROB_UP = 0.515  # >= resolution rule gives Up a structural edge

# --- Pre-window momentum parameters ---
MOMENTUM_WEIGHT = 0.15  # conservative: momentum is weak at 5-min scale
SECONDARY_WEIGHT_RATIO = 0.3
MOMENTUM_HALFLIFE_MIN = 10.0

# --- In-progress parameters ---
# How much to trust market vs model for live windows
# As more time passes, market becomes more accurate
MARKET_TRUST_AT_START = 0.3  # at t=0, trust market 30%
MARKET_TRUST_AT_END = 0.95   # at t=5min, trust market 95%
# Mean-reversion factor: how much extreme market prices overstate direction
MEAN_REVERSION_STRENGTH = 0.20  # pull 20% back toward 50% from extreme

# --- Probability bounds ---
PROB_FLOOR = 0.05
PROB_CEIL = 0.95

# --- Edge thresholds ---
DEFAULT_EDGE_THRESHOLD = 0.03
IN_PROGRESS_EDGE_THRESHOLD = 0.05  # higher bar for live windows (more uncertainty)

# --- Kelly ---
KELLY_FRACTION = 0.5  # half-Kelly


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
    in_progress: bool = False
    seconds_remaining: float = 0.0


def fair_prob_up_prewindow(
    snapshot: BTCSnapshot, minutes_ahead: float = 0.0
) -> float:
    """Calculate P(Up) for a window that hasn't started yet.

    Conservative: mainly base rate + weak momentum signal.
    """
    vol_annual = snapshot.volatility_1m
    if vol_annual <= 0:
        vol_annual = snapshot.volatility_1h
    if vol_annual <= 0:
        return BASE_PROB_UP

    tau = 5.0 / 525_960
    sigma_5m = vol_annual * math.sqrt(tau)
    if sigma_5m <= 0:
        return BASE_PROB_UP

    mom_5m = snapshot.momentum_5m
    mom_15m = snapshot.momentum_15m

    # Simple weighted momentum (no asymmetric hack — be honest)
    raw_mom = MOMENTUM_WEIGHT * mom_5m + (MOMENTUM_WEIGHT * SECONDARY_WEIGHT_RATIO) * mom_15m

    # Decay for future windows
    decay = 0.5 ** (minutes_ahead / MOMENTUM_HALFLIFE_MIN)
    mom_signal = raw_mom * decay

    base_shift = norm.ppf(BASE_PROB_UP) * sigma_5m
    z = (base_shift + mom_signal) / sigma_5m
    prob = float(norm.cdf(z))

    return max(PROB_FLOOR, min(PROB_CEIL, prob))


def fair_prob_up_inprogress(
    market_prob_up: float,
    seconds_elapsed: float,
    snapshot: BTCSnapshot | None = None,
) -> float:
    """Calculate P(Up) for a window that's currently live.

    The market price reflects trader consensus on the current direction.
    But markets overreact — if BTC dipped 0.05% in 2 minutes, the market
    might price Down at 85%, but mean-reversion makes the true probability
    closer to 65%.

    As more time passes (closer to resolution), the market becomes more
    accurate and we trust it more.
    """
    window_seconds = 300.0
    time_fraction = min(seconds_elapsed / window_seconds, 1.0)

    # How much to trust the market at this point in the window
    market_trust = MARKET_TRUST_AT_START + (
        MARKET_TRUST_AT_END - MARKET_TRUST_AT_START
    ) * time_fraction

    # Mean-reversion adjustment: pull extreme market prices back toward base
    # More reversion when less time has passed (market overreacts early)
    reversion_strength = MEAN_REVERSION_STRENGTH * (1 - time_fraction)
    adjusted_market = market_prob_up + reversion_strength * (BASE_PROB_UP - market_prob_up)

    # Blend market signal with our base rate
    model_prob = market_trust * adjusted_market + (1 - market_trust) * BASE_PROB_UP

    # If we have a snapshot, add a small momentum nudge
    if snapshot:
        mom_nudge = snapshot.momentum_5m * 0.05  # tiny weight
        model_prob += mom_nudge

    return max(PROB_FLOOR, min(PROB_CEIL, model_prob))


# Keep backward compatibility
def fair_prob_up(snapshot: BTCSnapshot, minutes_ahead: float = 0.0) -> float:
    """Calculate fair P(Up). Delegates to pre-window model."""
    return fair_prob_up_prewindow(snapshot, minutes_ahead)


def kelly_fraction_calc(prob: float, odds: float = 1.0) -> float:
    """Calculate Kelly criterion bet fraction (half-Kelly).

    f* = (p * (b + 1) - 1) / b, then halved for safety.
    """
    if prob <= 0.5:
        return 0.0
    f = (prob * (odds + 1) - 1) / odds
    return max(0.0, f * KELLY_FRACTION)


# Backward compat alias
kelly_fraction = kelly_fraction_calc


def evaluate_5m_market(
    market,  # FiveMinMarket
    snapshot: BTCSnapshot,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
) -> Signal:
    """Evaluate a single 5-minute market and produce a signal.

    Uses different strategies for pre-window vs in-progress markets.
    """
    is_live = market.is_in_progress
    market_up = market.mid if market.mid > 0 else market.up_price

    if is_live:
        # IN-PROGRESS: use market price + mean-reversion model
        model_up = fair_prob_up_inprogress(
            market_prob_up=market_up,
            seconds_elapsed=market.seconds_elapsed,
            snapshot=snapshot,
        )
        threshold = max(edge_threshold, IN_PROGRESS_EDGE_THRESHOLD)
    else:
        # PRE-WINDOW: use momentum model
        minutes_ahead = market.minutes_until_start
        model_up = fair_prob_up_prewindow(snapshot, minutes_ahead)
        threshold = edge_threshold

    edge = model_up - market_up

    # ONLY BUY UP. Backtest showed BUY DOWN is anti-predictive (25% win rate).
    # The >= resolution rule gives Up a structural edge; Down signals are noise.
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

    # Kelly sizing
    if buy_price > 0 and buy_price < 1 and side != "NO EDGE":
        net_odds = (1.0 - buy_price) / buy_price
        kf = kelly_fraction_calc(win_prob, net_odds)
    else:
        kf = 0.0

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
        seconds_remaining=market.seconds_until_end if is_live else 0,
    )
