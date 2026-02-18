# CLAUDE.md — Poly

Polymarket 5-minute BTC up/down trading system. Compares market odds against a backtest-calibrated momentum model to find profitable edges, sizes bets via Kelly criterion, and executes trades on Polymarket's CLOB.

## Project Structure

```
Poly/
├── CLAUDE.md              # This file
├── requirements.txt       # Python deps: requests, scipy, numpy
└── poly/                  # Main package
    ├── __init__.py
    ├── main.py            # CLI entry point — scan, watch, backtest, trade modes
    ├── btc.py             # BTC price, 1-min momentum, realized volatility (Kraken/CoinGecko)
    ├── polymarket.py      # 5-min market discovery via timestamp-based slug probing
    ├── model.py           # Fair-value model: asymmetric momentum → P(Up), Kelly sizing
    ├── backtest.py        # Backtesting engine: reconstruct snapshots, validate vs outcomes
    └── execute.py         # Order execution via py-clob-client (limit + market orders)
```

## How It Works

**Target**: Polymarket's 5-minute BTC up/down markets (`btc-updown-5m-{unix_ts}`).
Each market asks: "Will BTC go up or down in this 5-minute window?" Resolves via Chainlink BTC/USD feed. "Up" wins if `end_price >= start_price`.

**Pipeline**:
1. **Data**: Fetch 1-minute candles from Kraken → compute 5m/15m momentum + realized vol
2. **Discovery**: Generate `btc-updown-5m-{timestamp}` slugs (every 300s) and probe Gamma API in parallel
3. **Model**: Calculate `P(Up) = Φ((base_shift + momentum_signal) / σ_5min)` with asymmetric weighting
4. **Signal**: Compare model P(Up) to market price → flag when `|edge| > threshold`
5. **Sizing**: Kelly criterion (half-Kelly) with practical caps
6. **Execution**: Place orders via Polymarket CLOB API

## Key Commands

```bash
pip install -r requirements.txt                    # Core deps
pip install py-clob-client                         # Optional: only for live trading

# Scan — one-shot analysis of upcoming windows
python -m poly.main scan                           # Default: next 12 windows, 3% edge
python -m poly.main scan -e 0.02                   # Lower edge threshold to 2%
python -m poly.main scan -n 6                      # Scan next 6 windows (30 min)

# Watch — continuous scanning
python -m poly.main watch                          # Rescan every 60s
python -m poly.main watch -i 30                    # Rescan every 30s

# Backtest — validate model against resolved markets
python -m poly.main backtest                       # Last 6 hours
python -m poly.main backtest -H 12                 # Last 12 hours
python -m poly.main backtest -e 0.02               # Test with 2% edge threshold

# Trade — live trading (default: dry run)
python -m poly.main trade                          # Dry run, $1000 bankroll, $50 max
python -m poly.main trade -b 500 -m 25             # $500 bankroll, $25 max bet
python -m poly.main trade --live                   # REAL orders (requires POLY_PRIVATE_KEY)
```

## Model Design

### Backtest Findings

6-hour backtest over 71 resolved windows (64 actionable trades):
- **Overall win rate**: 56.2%, total P&L: +$4.00 (unit bets)
- **Strong Up momentum (>65%)**: 70% accuracy — highly predictive
- **Lean Down (35-45%)**: 60% accuracy — solid signal
- **Strong Down (<35%)**: ANTI-predictive — mean-reversion dominates
- **BUY UP**: 61.8% win rate vs **BUY DOWN**: 54.3%
- **Base rate**: ~53.5% of windows resolve Up (>= rule advantage)

### Model Parameters (`model.py`)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `MOMENTUM_WEIGHT_UP` | 0.45 | Up momentum is predictive (70% at high confidence) |
| `MOMENTUM_WEIGHT_DOWN` | 0.25 | Down extremes mean-revert; dampened weight |
| `SECONDARY_WEIGHT_RATIO` | 0.5 | 15-min momentum contributes at 50% of primary |
| `BASE_PROB_UP` | 0.515 | >= resolution rule + empirical 53.5% base rate |
| `MOMENTUM_HALFLIFE_MIN` | 10.0 | Momentum decays 50% every 10 min into the future |
| `PROB_FLOOR / CEIL` | 0.25 / 0.75 | Wider clamp range (was 30/70) |
| `DEFAULT_EDGE_THRESHOLD` | 0.03 | Minimum 3% edge to flag as actionable |

### Position Sizing

Half-Kelly criterion with practical caps:
- `f* = (p(b+1) - 1) / b` at half-Kelly
- Minimum bet: $5 (Polymarket minimum)
- Maximum: configurable (default $50), never > 10% of bankroll
- Kelly fraction only computed for actionable signals (|edge| > threshold)

## External APIs

| API | Base URL | Used For | Auth |
|-----|----------|----------|------|
| CoinGecko | `api.coingecko.com/api/v3` | BTC spot price (primary) | None |
| Coinbase | `api.coinbase.com/v2` | BTC spot price (fallback) | None |
| Kraken | `api.kraken.com/0/public` | 1-min & hourly OHLC candles | None |
| Polymarket Gamma | `gamma-api.polymarket.com` | Market discovery by slug | None |
| Polymarket CLOB | `clob.polymarket.com` | Order book + trading | HMAC (trading only) |

**Note**: Binance API is geo-blocked in some environments (HTTP 451). Kraken is the primary OHLC source.

**Note**: Gamma API search/filter params are unreliable (return unrelated markets). Discovery uses **exact slug lookups** with generated timestamps.

## Market Discovery

Slug pattern: `btc-updown-5m-{unix_timestamp}` where timestamp = start of 5-min window, aligned to 300-second boundaries. We generate timestamps for past/current/future windows and probe each in parallel via `ThreadPoolExecutor`.

Resolution detection: Gamma API `outcomePrices` field — `["1","0"]` = Up won, `["0","1"]` = Down won. Must check `closed=true`.

Also available (not yet implemented):
- 15-minute: `btc-updown-15m-{ts}`
- 4-hour: `btc-updown-4h-{ts}`
- Other assets: `eth-updown-*`, `sol-updown-*`, `xrp-updown-*`

## Execution Module

`execute.py` uses `py-clob-client` for authenticated order placement on Polygon (chain ID 137).

**Setup**:
```bash
pip install py-clob-client
export POLY_PRIVATE_KEY="0x..."      # Polygon wallet private key
export POLY_FUNDER="0x..."           # Optional: proxy wallet address
```

**Order types**:
- `place_limit_order()` — GTC limit order at specified or best-ask price
- `place_market_order()` — FOK market order for specified USD amount
- Client is lazily initialized (py-clob-client only imported when trading)

## Conventions

- **Python 3.11+** — uses `X | None` union syntax
- Pure Python: `requests`, `scipy`, `numpy` (+ optional `py-clob-client`)
- All timestamps are Unix seconds (UTC)
- Polymarket outcomes are "Up" / "Down" (not Yes/No) for 5-min markets
- Positive edge = Up is underpriced; negative edge = Down is underpriced
- Signal sides: `"BUY UP"`, `"BUY DOWN"`, `"NO EDGE"`

## Architecture Notes

- **Asymmetric momentum** is the core insight: Up momentum is predictive, Down momentum mean-reverts. This was discovered through backtest calibration analysis.
- **Momentum decay** is critical: current momentum predicts the *next* window, not ones 30+ min away. Without decay, the model over-signals on distant windows.
- **Resolution source is Chainlink**, not exchange spot prices. Small deviations between Chainlink and exchange prices could matter at the margin.
- **Backtest reconstructs historical snapshots** from 1-min Kraken candles at each window's start time, not from current market data.
- **Trade mode only evaluates the nearest window** to avoid stale-signal risk on further-out markets.
