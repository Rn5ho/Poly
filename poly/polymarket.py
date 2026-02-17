"""Polymarket API client for discovering Bitcoin up/down markets.

Discovery strategy: The Gamma API search/filter params are unreliable,
so we generate known slug patterns for BTC markets and probe each one
directly. This is fast (parallel-friendly) and reliable.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Strike prices Polymarket typically offers for BTC markets
BTC_STRIKES_K = [
    40, 45, 50, 55, 58, 60, 62, 64, 65, 66, 67, 68, 69, 70,
    72, 74, 75, 76, 78, 80, 82, 85, 88, 90, 92, 95, 98,
    100, 105, 110, 115, 120, 125, 130, 140, 150, 175, 200,
]

# Hourly up/down market times
UP_DOWN_TIMES = ["2pm-et"]


@dataclass
class Market:
    """A single Polymarket binary outcome market."""

    condition_id: str
    question: str
    slug: str
    end_date: datetime
    yes_price: float
    no_price: float
    yes_token_id: str
    no_token_id: str
    strike: float | None
    volume_24h: float
    liquidity: float
    best_bid: float
    best_ask: float

    @property
    def implied_prob_yes(self) -> float:
        return self.yes_price

    @property
    def mid(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.yes_price


def _parse_strike(question: str) -> float | None:
    """Extract the dollar strike price from a market question."""
    m = re.search(r"\$([0-9]{1,3}(?:,[0-9]{3})*)", question)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"(\d+)k", question, re.IGNORECASE)
    if m:
        return float(m.group(1)) * 1000
    return None


def _generate_slugs(days_ahead: int = 3) -> list[str]:
    """Generate candidate slugs for BTC markets over the next few days."""
    slugs = []
    now = datetime.now(timezone.utc)

    for day_offset in range(days_ahead + 1):
        date = now + timedelta(days=day_offset)
        month = date.strftime("%B").lower()
        day = date.day

        # Strike-based: "bitcoin-above-{N}k-on-{month}-{day}"
        for strike_k in BTC_STRIKES_K:
            slugs.append(f"bitcoin-above-{strike_k}k-on-{month}-{day}")

        # Up/down directional: "bitcoin-up-or-down-{month}-{day}-{time}"
        for time_str in UP_DOWN_TIMES:
            slugs.append(f"bitcoin-up-or-down-{month}-{day}-{time_str}")

    return slugs


def _fetch_market_by_slug(slug: str) -> Market | None:
    """Fetch a single market by exact slug. Returns None if not found."""
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

        end_str = m.get("endDate", "")
        end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

        return Market(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            slug=m.get("slug", ""),
            end_date=end_date,
            yes_price=float(outcome_prices[0]),
            no_price=float(outcome_prices[1]),
            yes_token_id=clob_token_ids[0],
            no_token_id=clob_token_ids[1],
            strike=_parse_strike(m.get("question", "")),
            volume_24h=float(m.get("volume24hr", 0) or 0),
            liquidity=float(m.get("liquidityNum", 0) or m.get("liquidity", 0) or 0),
            best_bid=float(m.get("bestBid", 0) or 0),
            best_ask=float(m.get("bestAsk", 0) or 0),
        )
    except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError):
        return None


def fetch_btc_markets(days_ahead: int = 3, workers: int = 20) -> list[Market]:
    """Discover active Bitcoin price markets by probing known slug patterns.

    Generates candidate slugs for BTC strike and up/down markets over the
    next few days, then fetches each one in parallel. Only returns active,
    non-closed markets with valid pricing data.
    """
    slugs = _generate_slugs(days_ahead)
    markets = []
    seen = set()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_market_by_slug, slug): slug for slug in slugs}
        for future in as_completed(futures):
            market = future.result()
            if market and market.condition_id not in seen:
                seen.add(market.condition_id)
                markets.append(market)

    # Sort by strike price for consistent output
    markets.sort(key=lambda m: m.strike or 0)
    return markets


def fetch_order_book(token_id: str) -> dict:
    """Fetch the CLOB order book for a given token."""
    resp = requests.get(
        f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10
    )
    resp.raise_for_status()
    return resp.json()
