"""Fair-value model for 5-minute Bitcoin up/down markets.

Core strategy: Outcome mean-reversion.
48-hour analysis of 575 windows found strong auto-correlation:
  - P(Up | prev Down) = 57.6%   → BUY UP
  - P(Up | prev Up)   = 49.2%   → skip
  - P(Up | prev 2xUp) = 44.3%   → skip
  - P(Up | base)      = 53.2%   → marginal

BUY DOWN is catastrophically anti-predictive (25% win rate) and removed.
Momentum at 5-min scale is near-random after look-ahead bias correction.
"""

import math
from dataclasses import dataclass

from poly.btc import BTCSnapshot

# --- Conditional probabilities from 48h analysis (575 windows) ---
PROB_UP_AFTER_DOWN = 0.576  # P(Up | prev was Down)
PROB_UP_AFTER_UP = 0.492  # P(Up | prev was Up)
PROB_UP_AFTER_2X_UP = 0.443  # P(Up | prev 2 were Up)
PROB_UP_BASE = 0.532  # unconditional base rate

# Edge thresholds
DEFAULT_EDGE_THRESHOLD = 0.03  # 3% for pre-window
IN_PROGRESS_EDGE_THRESHOLD = 0.05  # 5% for in-progress (market has info)


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


def conditional_prob_up(prev_outcomes: list[str]) -> float:
    """Calculate P(Up) conditioned on previous window outcomes.

    The core edge: after a Down window, P(Up) is 57.6% due to
    mean-reversion. After consecutive Ups, P(Up) drops to 44.3%.

    Args:
        prev_outcomes: list of recent outcomes ["Up", "Down", ...],
                      chronological order (oldest first).

    Returns:
        Conditional probability of Up for the next window.
    """
    if not prev_outcomes:
        return PROB_UP_BASE

    last = prev_outcomes[-1]

    # Check for 2+ consecutive Ups
    if (
        len(prev_outcomes) >= 2
        and prev_outcomes[-1] == "Up"
        and prev_outcomes[-2] == "Up"
    ):
        return PROB_UP_AFTER_2X_UP

    if last == "Down":
        return PROB_UP_AFTER_DOWN
    elif last == "Up":
        return PROB_UP_AFTER_UP
    else:
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


def kelly_fraction(prob: float, odds: float = 1.0) -> float:
    """Calculate half-Kelly bet fraction.

    f* = (p * (b + 1) - 1) / b  at half-Kelly.

    For Polymarket at price ~0.505: b = 0.495/0.505 ≈ 0.98
    So near even money.
    """
    if prob <= 0.5:
        return 0.0
    f = (prob * (odds + 1) - 1) / odds
    return max(0.0, f * 0.5)  # half-Kelly


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

    # Kelly sizing
    if buy_price > 0 and buy_price < 1 and side != "NO EDGE":
        net_odds = (1.0 - buy_price) / buy_price
        kf = kelly_fraction(win_prob, net_odds)
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
        seconds_remaining=secs_remaining,
        prev_outcome=prev_str,
    )
