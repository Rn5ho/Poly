"""Bitcoin price and volatility data from public exchange APIs.

Uses Kraken for OHLC data (volatility) and CoinGecko/Coinbase for spot price.
"""

import math
import time

import requests

KRAKEN_BASE = "https://api.kraken.com/0/public"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINBASE_BASE = "https://api.coinbase.com/v2"


def get_price() -> float:
    """Fetch current BTC/USD spot price.

    Tries CoinGecko first, falls back to Coinbase.
    """
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


def get_hourly_closes(hours: int = 168) -> list[float]:
    """Fetch recent hourly close prices from Kraken.

    Kraken OHLC returns: [time, open, high, low, close, vwap, volume, count]
    """
    since = int(time.time()) - hours * 3600
    resp = requests.get(
        f"{KRAKEN_BASE}/OHLC",
        params={"pair": "XBTUSD", "interval": 60, "since": since},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")

    candles = data["result"]["XXBTZUSD"]
    return [float(c[4]) for c in candles]  # index 4 = close


def realized_volatility(hours: int = 168) -> float:
    """Calculate annualized realized volatility from hourly log returns.

    Uses hourly closes over the given window, computes log returns,
    then annualizes: σ_annual = σ_hourly * sqrt(8760).
    """
    closes = get_hourly_closes(hours)
    if len(closes) < 2:
        raise ValueError("Not enough data to compute volatility")

    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    hourly_vol = math.sqrt(variance)

    # Annualize: 8760 hours per year
    return hourly_vol * math.sqrt(8760)
