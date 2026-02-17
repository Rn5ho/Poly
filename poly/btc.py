"""Bitcoin price and volatility data from public exchange APIs.

Uses Kraken for OHLC data (1-minute candles for momentum, hourly for volatility)
and CoinGecko/Coinbase for spot price.
"""

import math
import time
from dataclasses import dataclass

import requests

KRAKEN_BASE = "https://api.kraken.com/0/public"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINBASE_BASE = "https://api.coinbase.com/v2"


@dataclass
class BTCSnapshot:
    """Current BTC state for model input."""

    price: float
    momentum_5m: float  # log return over last 5 minutes
    momentum_15m: float  # log return over last 15 minutes
    volatility_1m: float  # 1-minute realized vol (annualized)
    volatility_1h: float  # hourly realized vol (annualized)
    recent_closes: list[float]  # last N 1-minute closes


def get_price() -> float:
    """Fetch current BTC/USD spot price."""
    try:
        resp = requests.get(
            f"{COINGECKO_BASE}/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["bitcoin"]["usd"])
    except Exception:
        resp = requests.get(
            f"{COINBASE_BASE}/prices/BTC-USD/spot", timeout=10
        )
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])


def _get_kraken_ohlc(interval: int, since: int) -> list[list]:
    """Fetch Kraken OHLC candles.

    Returns list of [time, open, high, low, close, vwap, volume, count].
    """
    resp = requests.get(
        f"{KRAKEN_BASE}/OHLC",
        params={"pair": "XBTUSD", "interval": interval, "since": since},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")
    return data["result"]["XXBTZUSD"]


def get_minute_closes(minutes: int = 60) -> list[float]:
    """Fetch recent 1-minute close prices from Kraken."""
    since = int(time.time()) - minutes * 60
    candles = _get_kraken_ohlc(interval=1, since=since)
    return [float(c[4]) for c in candles]


def get_hourly_closes(hours: int = 168) -> list[float]:
    """Fetch recent hourly close prices from Kraken."""
    since = int(time.time()) - hours * 3600
    candles = _get_kraken_ohlc(interval=60, since=since)
    return [float(c[4]) for c in candles]


def _log_returns(prices: list[float]) -> list[float]:
    return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]


def _realized_vol(log_rets: list[float], periods_per_year: float) -> float:
    """Annualized realized volatility from log returns."""
    if len(log_rets) < 2:
        return 0.0
    n = len(log_rets)
    mean = sum(log_rets) / n
    variance = sum((r - mean) ** 2 for r in log_rets) / (n - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def get_snapshot() -> BTCSnapshot:
    """Build a full BTC snapshot: price, momentum, and volatility."""
    price = get_price()

    # 1-minute candles for short-term signals (last 60 minutes)
    minute_closes = get_minute_closes(60)

    # Momentum: log return over last 5 and 15 candles
    momentum_5m = 0.0
    momentum_15m = 0.0
    if len(minute_closes) >= 6:
        momentum_5m = math.log(minute_closes[-1] / minute_closes[-6])
    if len(minute_closes) >= 16:
        momentum_15m = math.log(minute_closes[-1] / minute_closes[-16])

    # 1-minute realized vol (annualized: 525,960 minutes/year)
    minute_rets = _log_returns(minute_closes)
    vol_1m = _realized_vol(minute_rets, 525_960)

    # Hourly realized vol (annualized: 8,760 hours/year)
    hourly_closes = get_hourly_closes(168)
    hourly_rets = _log_returns(hourly_closes)
    vol_1h = _realized_vol(hourly_rets, 8_760)

    return BTCSnapshot(
        price=price,
        momentum_5m=momentum_5m,
        momentum_15m=momentum_15m,
        volatility_1m=vol_1m,
        volatility_1h=vol_1h,
        recent_closes=minute_closes,
    )


def realized_volatility(hours: int = 168) -> float:
    """Calculate annualized realized volatility from hourly log returns."""
    closes = get_hourly_closes(hours)
    if len(closes) < 2:
        raise ValueError("Not enough data to compute volatility")
    return _realized_vol(_log_returns(closes), 8_760)
