"""Fair-value model for 5-minute Bitcoin up/down markets.

Core finding: OUTCOME MEAN-REVERSION is the primary edge.

48-hour analysis (575 windows) showed:
  - P(Up | prev Down) = 57.6% — strong signal, BUY UP
  - P(Up | prev Up)   = 49.2% — no edge
  - P(Up | prev 2x Up) = 44.3% — anti-predictive, SKIP
  - Base rate: 53.2% Up (>= resolution rule)

Strategy:
  1. After a Down window → BUY UP (57.6% expected win rate)
  2. After a single Up → BUY UP at reduced confidence (base rate only)
  3. After 2+ consecutive Ups → SKIP (44.3% Up rate = negative edge)

Also supports in-progress window evaluation for live markets
where pricing has overreacted to early movement.
"""

import math
import time
from dataclasses import dataclass

from scipy.stats import norm

from poly.btc import BTCSnapshot


# --- Conditional probabilities from 48h analysis ---
PROB_UP_AFTER_DOWN = 0.576    # P(Up | prev was Down)
PROB_UP_AFTER_UP = 0.492      # P(Up | prev was Up)
PROB_UP_AFTER_2X_UP = 0.443   # P(Up | prev 2 were Up)
PROB_UP_BASE = 0.532          # unconditional base rate

# --- In-progress parameters ---
MARKET_TRUST_AT_START = 0.3
MARKET_TRUST_AT_END = 0.95
MEAN_REVERSION_STRENGTH = 0.20

# --- Bounds ---
PROB_FLOOR = 0.05
PROB_CEIL = 0.95

# --- Edge thresholds ---
DEFAULT_EDGE_THRESHOLD = 0.03
IN_PROGRESS_EDGE_THRESHOLD = 0.05

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
    prev_outcome: str = ""  # "Up", "Down", or "" if unknown


def conditional_prob_up(
    prev_outcomes: list[str],
) -> float:
    """Calculate P(Up) based on previous window outcomes.

    This is the core model: mean-reversion in 5-min BTC outcomes.

    Args:
        prev_outcomes: list of recent outcomes, most recent last.
                       e.g. ["Up", "Down"] means second-to-last was Up,
                       last was Down.
    """
    if not prev_outcomes:
        return PROB_UP_BASE

    last = prev_outcomes[-1]

    # Check for 2+ consecutive Ups
    if len(prev_outcomes) >= 2 and prev_outcomes[-1] == "Up" and prev_outcomes[-2] == "Up":
        return PROB_UP_AFTER_2X_UP

    if last == "Down":
        return PROB_UP_AFTER_DOWN
    elif last == "Up":
        return PROB_UP_AFTER_UP
    else:
        return PROB_UP_BASE


def fair_prob_up_inprogress(
    market_prob_up: float,
    seconds_elapsed: float,
    snapshot: BTCSnapshot | None = None,
) -> float:
    """Calculate P(Up) for a window that's currently live.

    Markets overreact to early price movement. Apply mean-reversion
    that fades as the window nears resolution.
    """
    window_seconds = 300.0
    time_fraction = min(seconds_elapsed / window_seconds, 1.0)

    market_trust = MARKET_TRUST_AT_START + (
        MARKET_TRUST_AT_END - MARKET_TRUST_AT_START
    ) * time_fraction

    reversion_strength = MEAN_REVERSION_STRENGTH * (1 - time_fraction)
    adjusted_market = market_prob_up + reversion_strength * (PROB_UP_BASE - market_prob_up)

    model_prob = market_trust * adjusted_market + (1 - market_trust) * PROB_UP_BASE

    return max(PROB_FLOOR, min(PROB_CEIL, model_prob))


# Backward compatibility
def fair_prob_up(snapshot: BTCSnapshot, minutes_ahead: float = 0.0) -> float:
    """Legacy function. Returns base rate probability."""
    return PROB_UP_BASE


def kelly_fraction_calc(prob: float, odds: float = 1.0) -> float:
    """Half-Kelly bet fraction."""
    if prob <= 0.5:
        return 0.0
    f = (prob * (odds + 1) - 1) / odds
    return max(0.0, f * KELLY_FRACTION)


kelly_fraction = kelly_fraction_calc


def evaluate_5m_market(
    market,  # FiveMinMarket
    snapshot: BTCSnapshot,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    prev_outcomes: list[str] | None = None,
) -> Signal:
    """Evaluate a 5-minute market using the conditional mean-reversion model.

    Args:
        market: The market to evaluate.
        snapshot: Current BTC state.
        edge_threshold: Minimum edge to flag as actionable.
        prev_outcomes: List of recent window outcomes ["Up", "Down", ...].
                       Most recent last. Used for conditional probability.
    """
    is_live = market.is_in_progress
    market_up = market.mid if market.mid > 0 else market.up_price

    if is_live:
        model_up = fair_prob_up_inprogress(
            market_prob_up=market_up,
            seconds_elapsed=market.seconds_elapsed,
            snapshot=snapshot,
        )
        threshold = max(edge_threshold, IN_PROGRESS_EDGE_THRESHOLD)
    else:
        # Conditional model: use previous outcomes
        model_up = conditional_prob_up(prev_outcomes or [])
        threshold = edge_threshold

    edge = model_up - market_up

    # BUY UP only. Skip after 2+ consecutive Ups (negative edge).
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
        seconds_remaining=market.seconds_until_end if is_live else 0,
        prev_outcome=prev_str,
    )
