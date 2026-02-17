# CLAUDE.md — Poly

Polymarket 5-minute BTC up/down mispricing scanner. Compares market odds against a momentum-based fair-value model to find profitable edges on Polymarket's rolling 5-minute Bitcoin markets.

## Project Structure

```
Poly/
├── CLAUDE.md              # This file
├── requirements.txt       # Python deps: requests, scipy, numpy
└── poly/                  # Main package
    ├── __init__.py
    ├── main.py            # CLI entry point — scan, watch mode
    ├── btc.py             # BTC price, 1-min momentum, realized volatility (Kraken/CoinGecko)
    ├── polymarket.py      # 5-min market discovery via timestamp-based slug probing
    └── model.py           # Fair-value model: momentum + vol → P(Up), with time decay
```

## How It Works

**Target**: Polymarket's 5-minute BTC up/down markets (`btc-updown-5m-{unix_ts}`).
Each market asks: "Will BTC go up or down in this 5-minute window?" Resolves via Chainlink BTC/USD feed. "Up" wins if `end_price >= start_price`.

**Model**:
1. Fetch 1-minute candles from Kraken → compute 5m/15m momentum + realized vol
2. Discover upcoming markets by generating `btc-updown-5m-{timestamp}` slugs (every 300s)
3. For each window, calculate `P(Up) = Φ(momentum_signal / σ_5min)`:
   - Momentum signal = weighted combo of 5m and 15m log returns
   - Signal decays exponentially for future windows (half-life: 10 min)
   - Near windows get full signal; windows 30+ min away → ~50/50
4. Compare model P(Up) to market price. Flag when `|edge| > threshold`

**Key insight**: These markets are mostly priced at 50/50. Short-term momentum (autocorrelation in 1-min returns) provides a small but consistent edge, especially for the next 1-2 windows.

## Key Commands

```bash
pip install -r requirements.txt          # Install deps
python -m poly.main                      # Single scan (next hour of windows)
python -m poly.main -e 0.02              # Lower edge threshold to 2%
python -m poly.main -n 6                 # Scan next 6 windows (30 min)
python -m poly.main -w                   # Watch mode: rescan every 60s
python -m poly.main -w -i 30             # Watch mode: rescan every 30s
```

## External APIs (no auth required)

| API | Base URL | Used For |
|-----|----------|----------|
| CoinGecko | `api.coingecko.com/api/v3` | BTC spot price (primary) |
| Coinbase | `api.coinbase.com/v2` | BTC spot price (fallback) |
| Kraken | `api.kraken.com/0/public` | 1-min & hourly OHLC candles |
| Polymarket Gamma | `gamma-api.polymarket.com` | Market discovery by slug |
| Polymarket CLOB | `clob.polymarket.com` | Order book data |

**Note**: Binance API is geo-blocked in some environments (451). Kraken is the primary OHLC source.

**Note**: Gamma API search/filter params are unreliable. Discovery uses **exact slug lookups** with generated timestamps.

## Market Discovery

Slug pattern: `btc-updown-5m-{unix_timestamp}` where timestamp = start of 5-min window, aligned to 300-second boundaries. We generate timestamps for past/current/future windows and probe each in parallel via `ThreadPoolExecutor`.

Also available (not yet implemented):
- 15-minute: `btc-updown-15m-{ts}`
- 4-hour: `btc-updown-4h-{ts}`
- Other assets: `eth-updown-*`, `sol-updown-*`, `xrp-updown-*`

## Model Tuning

Key parameters in `model.py`:
- `MOMENTUM_WEIGHT = 0.4` — how much to trust the raw momentum signal (0–1)
- `MOMENTUM_HALFLIFE_MIN = 10.0` — momentum decays 50% every 10 min into the future
- `DEFAULT_EDGE_THRESHOLD = 0.03` — minimum edge to flag (3%)

These are conservative defaults. A more aggressive setup: `MOMENTUM_WEIGHT=0.6, edge=0.02`.

## Conventions

- **Python 3.11+** — uses `X | None` union syntax
- Pure Python: `requests`, `scipy`, `numpy`
- All timestamps are Unix seconds (UTC)
- Polymarket outcomes are "Up" / "Down" (not Yes/No) for 5-min markets
- Positive edge = Up is underpriced; negative edge = Down is underpriced

## Architecture Notes

- **Signal generation only** — no trading/execution. Placing trades requires Polymarket wallet + CLOB API auth.
- **Momentum decay** is critical: current momentum predicts the *next* window, not ones 30 min away. Without decay, the model over-signals on distant windows.
- **Resolution source is Chainlink**, not exchange spot prices. Small deviations between Chainlink and exchange prices could matter at the margin.
