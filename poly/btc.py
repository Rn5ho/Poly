"""Bitcoin price and volatility data from public APIs.

Uses Chainlink BTC/USD oracle on Polygon as primary price source (same
oracle Polymarket uses for market resolution). Falls back to CoinGecko/Coinbase.

Uses Kraken for OHLC data (1-minute candles for momentum, hourly for volatility).
"""

import math
import time
from dataclasses import dataclass

import requests

KRAKEN_BASE = "https://api.kraken.com/0/public"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINBASE_BASE = "https://api.coinbase.com/v2"

# Chainlink BTC/USD Price Feed on Polygon (same oracle used by Polymarket)
# Contract: 0xc907E116054Ad103354f2D350FD2514433D57F6f
# Function: latestRoundData() → (roundId, answer, startedAt, updatedAt, answeredInRound)
# answer has 8 decimal places (divide by 1e8 to get USD)
CHAINLINK_CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
CHAINLINK_SELECTOR = "0xfeaf968c"  # keccak256("latestRoundData()")[:4]
POLYGON_RPC_URLS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]


@dataclass
class BTCSnapshot:
    """Current BTC state for model input."""

    price: float
    chainlink_price: float | None  # Chainlink oracle price (resolution source)
    momentum_5m: float  # log return over last 5 minutes
    momentum_15m: float  # log return over last 15 minutes
    volatility_1m: float  # 1-minute realized vol (annualized)
    volatility_1h: float  # hourly realized vol (annualized)
    recent_closes: list[float]  # last N 1-minute closes


def get_chainlink_price() -> float | None:
    """Fetch BTC/USD from Chainlink oracle on Polygon.

    This is the EXACT price feed Polymarket uses to resolve markets.
    Returns price in USD, or None if all RPC endpoints fail.
    """
    for rpc_url in POLYGON_RPC_URLS:
        try:
            resp = requests.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [
                        {"to": CHAINLINK_CONTRACT, "data": CHAINLINK_SELECTOR},
                        "latest",
                    ],
                    "id": 1,
                },
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json().get("result", "")
            if not result or result == "0x":
                continue

            # ABI decode: skip '0x', then 5 x 32-byte (64-char) words
            # Word 0: roundId, Word 1: answer (int256), Word 2: startedAt, ...
            hex_data = result[2:]
            if len(hex_data) < 320:  # need at least 5 words
                continue

            answer_hex = hex_data[64:128]
            answer = int(answer_hex, 16)
            # Handle signed int256 (if negative, which shouldn't happen for price)
            if answer > 2**255:
                answer -= 2**256

            price = answer / 1e8  # 8 decimal places
            if price > 1000:  # sanity check: BTC should be > $1000
                return price
        except Exception:
            continue
    return None


def get_price() -> float:
    """Fetch current BTC/USD spot price.

    Tries Chainlink (resolution oracle) first, falls back to exchanges.
    """
    # Try Chainlink first — this is what Polymarket resolves against
    chainlink = get_chainlink_price()
    if chainlink:
        return chainlink

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
    chainlink = get_chainlink_price()
    price = chainlink if chainlink else get_price()

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
        chainlink_price=chainlink,
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
