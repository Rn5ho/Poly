"""Polymarket API client for 5-minute Bitcoin up/down markets.

Market format:
  Slug:     btc-updown-5m-{unix_timestamp}
  Outcomes: ["Up", "Down"]
  Rule:     "Up" if end_price >= start_price (Chainlink BTC/USD)
  Windows:  Every 300 seconds, 24/7

Discovery: Generate timestamps for current and upcoming 5-minute
windows, probe each slug directly via the Gamma API.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

WINDOW_SECONDS = 300  # 5 minutes


@dataclass
class FiveMinMarket:
    """A single 5-minute BTC up/down market."""

    condition_id: str
    question: str
    slug: str
    window_start: int  # unix timestamp
    window_end: int
    up_price: float  # market price of "Up" token
    down_price: float  # market price of "Down" token
    up_token_id: str
    down_token_id: str
    best_bid: float
    best_ask: float
    spread: float
    volume: float
    liquidity: float
    active: bool
    closed: bool

    @property
    def seconds_until_start(self) -> float:
        return max(self.window_start - time.time(), 0)

    @property
    def seconds_until_end(self) -> float:
        return max(self.window_end - time.time(), 0)

    @property
    def seconds_elapsed(self) -> float:
        """Seconds elapsed since window start (0 if not started)."""
        return max(time.time() - self.window_start, 0)

    @property
    def minutes_until_start(self) -> float:
        return self.seconds_until_start / 60

    @property
    def is_in_progress(self) -> bool:
        """True if this window is currently live (started but not ended)."""
        now = time.time()
        return self.window_start <= now < self.window_end and not self.closed

    @property
    def is_tradeable(self) -> bool:
        return self.active and not self.closed and self.liquidity > 0

    @property
    def mid(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.up_price


def _fetch_5m_market(timestamp: int) -> FiveMinMarket | None:
    """Fetch a single 5-minute market by its window start timestamp."""
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
        outcome_prices = json.loads(m.get("outcomePrices", "[]"))
        clob_token_ids = json.loads(m.get("clobTokenIds", "[]"))
        if len(outcome_prices) < 2 or len(clob_token_ids) < 2:
            return None

        return FiveMinMarket(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            slug=slug,
            window_start=timestamp,
            window_end=timestamp + WINDOW_SECONDS,
            up_price=float(outcome_prices[0]),
            down_price=float(outcome_prices[1]),
            up_token_id=clob_token_ids[0],
            down_token_id=clob_token_ids[1],
            best_bid=float(m.get("bestBid", 0) or 0),
            best_ask=float(m.get("bestAsk", 0) or 0),
            spread=float(m.get("spread", 0) or 0),
            volume=float(m.get("volume24hr", 0) or 0),
            liquidity=float(m.get("liquidityNum", 0) or m.get("liquidity", 0) or 0),
            active=bool(m.get("active")),
            closed=bool(m.get("closed")),
        )
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError):
        return None


def fetch_5m_markets(
    past_windows: int = 3,
    future_windows: int = 12,
    workers: int = 20,
) -> list[FiveMinMarket]:
    """Discover 5-minute BTC up/down markets around the current time.

    Args:
        past_windows: Number of past 5-min windows to check.
        future_windows: Number of upcoming 5-min windows to check.
        workers: Thread pool size for parallel fetches.
    """
    now = int(time.time())
    current_window = (now // WINDOW_SECONDS) * WINDOW_SECONDS

    timestamps = [
        current_window + (i * WINDOW_SECONDS)
        for i in range(-past_windows, future_windows + 1)
    ]

    markets = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_5m_market, ts): ts for ts in timestamps}
        for future in as_completed(futures):
            market = future.result()
            if market:
                markets.append(market)

    markets.sort(key=lambda m: m.window_start)
    return markets


def fetch_order_book(token_id: str) -> dict:
    """Fetch the CLOB order book for a given token."""
    resp = requests.get(
        f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10
    )
    resp.raise_for_status()
    return resp.json()
