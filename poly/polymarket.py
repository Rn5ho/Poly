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


def _fetch_resolved_outcome(timestamp: int, verbose: bool = False) -> str | None:
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
            if verbose:
                print(f"    [resolve] {slug}: no results from API", flush=True)
            return None

        m = results[0]
        closed = m.get("closed")
        if not closed:
            if verbose:
                prices_raw = m.get("outcomePrices", "[]")
                print(
                    f"    [resolve] {slug}: not closed (closed={closed!r}, "
                    f"prices={prices_raw})",
                    flush=True,
                )
            return None

        outcome_prices = json.loads(m.get("outcomePrices", "[]"))
        if len(outcome_prices) < 2:
            if verbose:
                print(f"    [resolve] {slug}: closed but <2 outcome prices", flush=True)
            return None

        # Use float comparison for robustness (API may return "1", "1.0", etc.)
        try:
            up_price = float(outcome_prices[0])
            down_price = float(outcome_prices[1])
        except (ValueError, TypeError):
            if verbose:
                print(
                    f"    [resolve] {slug}: can't parse prices {outcome_prices}",
                    flush=True,
                )
            return None

        if up_price > 0.99:
            return "Up"
        elif down_price > 0.99:
            return "Down"

        if verbose:
            print(
                f"    [resolve] {slug}: closed but prices not settled "
                f"(up={up_price}, down={down_price})",
                flush=True,
            )
        return None
    except Exception as e:
        if verbose:
            print(f"    [resolve] {slug}: exception: {e}", flush=True)
        return None


def fetch_recent_outcomes(n: int = 3) -> list[str]:
    """Fetch resolved outcomes for the last N completed 5-minute windows.

    Returns list of outcomes ["Up", "Down", ...] in chronological order
    (oldest first). Used for the conditional mean-reversion model.
    """
    now = int(time.time())
    current_window = (now // WINDOW_SECONDS) * WINDOW_SECONDS

    # Check last n+2 windows (some may not be resolved yet), skip current
    timestamps = [
        current_window - (i * WINDOW_SECONDS)
        for i in range(1, n + 3)
    ]

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(timestamps), 10)) as pool:
        futures = {pool.submit(_fetch_resolved_outcome, ts): ts for ts in timestamps}
        for future in as_completed(futures):
            ts = futures[future]
            result = future.result()
            if result:
                results[ts] = result

    # Sort chronologically and take the last n
    outcomes = []
    for ts in sorted(results.keys()):
        outcomes.append(results[ts])

    return outcomes[-n:] if len(outcomes) > n else outcomes


def fetch_order_book(token_id: str) -> dict:
    """Fetch the CLOB order book for a given token."""
    resp = requests.get(
        f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10
    )
    resp.raise_for_status()
    return resp.json()


@dataclass
class LiveBook:
    """Snapshot of the live order book for a token."""

    best_bid: float
    best_ask: float
    bid_size: float  # total shares at best bid
    ask_size: float  # total shares at best ask
    spread: float
    midpoint: float

    @property
    def is_valid(self) -> bool:
        return self.best_bid > 0 and self.best_ask > 0 and self.best_ask > self.best_bid


def fetch_live_book(token_id: str) -> LiveBook | None:
    """Fetch current best bid/ask from the CLOB order book.

    Returns a LiveBook with live prices, or None on failure.
    Use this instead of hardcoded price constants.
    """
    try:
        book = fetch_order_book(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return None

        # Best bid = highest bid price, best ask = lowest ask price
        best_bid_level = max(bids, key=lambda b: float(b["price"]))
        best_ask_level = min(asks, key=lambda a: float(a["price"]))

        best_bid = float(best_bid_level["price"])
        best_ask = float(best_ask_level["price"])
        bid_size = float(best_bid_level["size"])
        ask_size = float(best_ask_level["size"])

        return LiveBook(
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            spread=best_ask - best_bid,
            midpoint=(best_bid + best_ask) / 2,
        )
    except (requests.RequestException, ValueError, KeyError):
        return None


def fetch_midpoint(token_id: str) -> float | None:
    """Fetch the midpoint price from the CLOB.

    Uses GET /midpoint?token_id=<id> — single lightweight call.
    Returns midpoint as float, or None on failure.
    """
    try:
        resp = requests.get(
            f"{CLOB_BASE}/midpoint",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        mid = float(data.get("mid", 0))
        return mid if mid > 0 else None
    except (requests.RequestException, ValueError):
        return None


def fetch_price_history(
    token_id: str,
    start_ts: int | None = None,
    end_ts: int | None = None,
    fidelity: int = 1,
) -> list[dict]:
    """Fetch historical price data for a token.

    Uses GET /prices-history?market=<token_id>&startTs=X&endTs=Y&fidelity=N.
    Returns list of {"t": unix_ts, "p": price} dicts.

    Args:
        token_id: CLOB token ID.
        start_ts: Start of time range (unix seconds).
        end_ts: End of time range (unix seconds).
        fidelity: Resolution in minutes (1 = 1-minute candles).
    """
    try:
        params: dict = {"market": token_id, "fidelity": fidelity}
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts

        resp = requests.get(
            f"{CLOB_BASE}/prices-history",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("history", [])
    except (requests.RequestException, ValueError):
        return []
