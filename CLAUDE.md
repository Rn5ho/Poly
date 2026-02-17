# CLAUDE.md — Poly

Polymarket 5-minute BTC up/down trading system. Exploits outcome mean-reversion (P(Up|prev Down) = 57.6%) with conditional probability model, Kelly sizing, and optional live execution on Polymarket's CLOB.

## Project Structure

```
Poly/
├── CLAUDE.md              # This file
├── requirements.txt       # Python deps: requests, scipy, numpy
└── poly/                  # Main package
    ├── __init__.py
    ├── main.py            # CLI entry point — scan, watch, backtest, trade modes
    ├── btc.py             # BTC price (Chainlink primary), momentum, volatility (Kraken)
    ├── polymarket.py      # 5-min market discovery + outcome fetching via Gamma API
    ├── model.py           # Conditional mean-reversion model + in-progress window model
    ├── backtest.py        # Backtesting engine: outcome-based conditional strategy validation
    └── execute.py         # Order execution via py-clob-client (limit + market orders)
```

## How It Works

**Target**: Polymarket's 5-minute BTC up/down markets (`btc-updown-5m-{unix_ts}`).
Each market asks: "Will BTC go up or down in this 5-minute window?" Resolves via Chainlink BTC/USD feed. "Up" wins if `end_price >= start_price`.

**Pipeline**:
1. **Discovery**: Generate `btc-updown-5m-{timestamp}` slugs (every 300s) and probe Gamma API in parallel
2. **Outcome History**: Fetch resolved outcomes for recent windows via Gamma API
3. **Model**: Conditional P(Up) based on previous window outcomes (mean-reversion)
4. **Signal**: Compare model P(Up) to market price → flag when edge > threshold
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
python -m poly.main backtest -H 48 -b 100          # 48 hours, $100 bankroll
python -m poly.main backtest -e 0.02               # Test with 2% edge threshold

# Trade — live trading (default: dry run)
python -m poly.main trade                          # Dry run, $1000 bankroll, $50 max
python -m poly.main trade -b 500 -m 25             # $500 bankroll, $25 max bet
python -m poly.main trade --live                   # REAL orders (requires POLY_PRIVATE_KEY)
```

## Model Design

### Core Finding: Outcome Mean-Reversion

48-hour analysis (575 windows) revealed strong auto-correlation in 5-minute BTC outcomes:

| Condition | P(Up) | Edge vs Market (~50.5%) | Action |
|-----------|-------|------------------------|--------|
| After Down | 57.6% | +7.1% | **BUY UP** |
| After single Up | 49.2% | -1.3% | Skip |
| After 2+ consecutive Ups | 44.3% | -6.2% | Skip |
| Base rate (unconditional) | 53.2% | +2.7% | Marginal |

**Strategy**: BUY UP only after a Down outcome. Skip all other conditions.

### 48-Hour Backtest Results

574 windows, 269 actionable trades:
- **Win rate**: 57.6%
- **$100 → $774** (+674% ROI)
- **Peak**: $1,019 | **Max drawdown**: 45%
- **Avg bet**: $28.56 | **P&L per trade**: +$2.51

### What Doesn't Work (Dead Ends)

- **Momentum at 5-min scale**: Near-random, 42-46% win rate after look-ahead bias fix
- **BUY DOWN**: 25% win rate — catastrophically anti-predictive
- **Chainlink latency arb**: Divergence < 0.05%, too small to exploit
- **Order book imbalance**: Single symmetric market maker, no signal

### Model Parameters (`model.py`)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `PROB_UP_AFTER_DOWN` | 0.576 | P(Up \| prev was Down) — primary edge |
| `PROB_UP_AFTER_UP` | 0.492 | P(Up \| prev was Up) — no edge |
| `PROB_UP_AFTER_2X_UP` | 0.443 | P(Up \| prev 2 were Up) — negative edge |
| `PROB_UP_BASE` | 0.532 | Unconditional base rate |
| `DEFAULT_EDGE_THRESHOLD` | 0.03 | Minimum 3% edge to flag as actionable |
| `IN_PROGRESS_EDGE_THRESHOLD` | 0.05 | Higher threshold for live windows |

### In-Progress Window Model

For windows currently live, markets show extreme skew (e.g. 6¢/94¢). The model applies:
- Market trust increases linearly from 30% → 95% as window progresses
- Mean-reversion toward base rate, fading as resolution approaches
- Higher edge threshold (5%) since these prices reflect real information

### Position Sizing

Half-Kelly criterion with practical caps:
- `f* = (p(b+1) - 1) / b` at half-Kelly
- Minimum bet: $5 (Polymarket minimum)
- Maximum: configurable (default $50), never > 10% of bankroll
- Kelly fraction only computed for actionable signals (edge > threshold)

## External APIs

| API | Base URL | Used For | Auth |
|-----|----------|----------|------|
| Chainlink (Polygon) | `rpc.ankr.com/polygon` | BTC/USD oracle (resolution source) | None |
| CoinGecko | `api.coingecko.com/api/v3` | BTC spot price (fallback) | None |
| Coinbase | `api.coinbase.com/v2` | BTC spot price (fallback) | None |
| Kraken | `api.kraken.com/0/public` | 1-min & hourly OHLC candles | None |
| Polymarket Gamma | `gamma-api.polymarket.com` | Market discovery + outcome resolution | None |
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
- Positive edge = model P(Up) > market P(Up) — Up is underpriced
- Signal sides: `"BUY UP"` or `"NO EDGE"` (BUY DOWN removed — anti-predictive)

## Architecture Notes

- **Outcome mean-reversion** is the core edge: after a Down window, P(Up) = 57.6%. This is a structural pattern, not momentum-based.
- **Chainlink is the resolution oracle** on Polygon (`0xc907E116054Ad103354f2D350FD2514433D57F6f`). The system fetches Chainlink directly as primary price source.
- **Backtest is purely outcome-based** — no candle reconstruction needed. The conditional model only needs the sequence of previous window outcomes.
- **Scan/trade modes fetch recent outcomes** from the Gamma API to build the outcome history needed for conditional predictions.
- **In-progress windows** have a separate model that blends market price with mean-reversion, trusting the market more as resolution approaches.
- **Trade mode only evaluates the nearest window** to avoid stale-signal risk on further-out markets.
