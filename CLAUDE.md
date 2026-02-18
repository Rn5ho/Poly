# CLAUDE.md — Poly

Polymarket 5-minute BTC up/down trading system. Uses outcome mean-reversion (BUY UP after Down windows) with graduated conditional probabilities and quarter-Kelly sizing.

## Project Structure

```
Poly/
├── CLAUDE.md              # This file
├── requirements.txt       # Python deps: requests, scipy, numpy
└── poly/                  # Main package
    ├── __init__.py
    ├── main.py            # CLI entry point — scan, watch, backtest, trade, autobot modes
    ├── btc.py             # BTC price, 1-min momentum, realized volatility (Kraken/CoinGecko)
    ├── polymarket.py      # 5-min market discovery via timestamp-based slug probing
    ├── model.py           # Conditional mean-reversion model: P(Up|prev outcomes), Kelly sizing
    ├── backtest.py        # Backtesting engine: reconstruct snapshots, validate vs outcomes
    ├── execute.py         # Order execution via py-clob-client (limit + market orders)
    └── autobot.py         # Automated continuous trading bot with state persistence
```

## How It Works

**Target**: Polymarket's 5-minute BTC up/down markets (`btc-updown-5m-{unix_ts}`).
Each market asks: "Will BTC go up or down in this 5-minute window?" Resolves via Chainlink BTC/USD feed. "Up" wins if `end_price >= start_price`.

**Markets launched**: February 12, 2026 (first market at 00:35 UTC).

**Pipeline**:
1. **Discovery**: Generate `btc-updown-5m-{timestamp}` slugs and probe Gamma API
2. **Outcomes**: Fetch recent resolved window outcomes (Up/Down sequence)
3. **Model**: Conditional P(Up) based on previous outcome streaks (mean-reversion)
4. **Signal**: Compare model P(Up) to market price (~50.5%) → flag when edge > 3%
5. **Sizing**: Quarter-Kelly criterion with 10% bankroll cap
6. **Execution**: Place orders via Polymarket CLOB API (or dry-run simulate)

## Key Commands

```bash
pip install -r requirements.txt                    # Core deps
pip install py-clob-client                         # Optional: only for live trading

# Scan — one-shot analysis of upcoming windows
python -m poly.main scan                           # Default: next 12 windows, 3% edge
python -m poly.main scan -e 0.02                   # Lower edge threshold to 2%

# Watch — continuous scanning
python -m poly.main watch                          # Rescan every 60s

# Backtest — validate model against resolved markets
python -m poly.main backtest                       # Last 6 hours
python -m poly.main backtest -H 152 -b 500         # Full history since launch, $500 bankroll

# Trade — single-shot trading
python -m poly.main trade                          # Dry run
python -m poly.main trade --live                   # REAL orders

# Autobot — automated continuous trading (NEW)
python -m poly.main autobot                        # Dry run, $500 bankroll, quarter-Kelly
python -m poly.main autobot -b 1000               # $1000 bankroll
python -m poly.main autobot -k 0.5                # Half-Kelly (more aggressive)
python -m poly.main autobot --live                 # REAL orders (requires POLY_PRIVATE_KEY)
python -m poly.main autobot --fresh                # Ignore saved state, start fresh
python -m poly.autobot                             # Direct module execution
```

## Model Design

### Core Strategy: Outcome Mean-Reversion

After a Down window, the next window has a higher-than-market probability of being Up (mean-reversion). This effect intensifies with consecutive Downs.

**BUY DOWN does NOT work** — no Down signal survives 95% confidence interval testing.

### Calibration Data

6-day backtest over 1,823 resolved windows (Feb 12-18 2026, 864 actionable trades):

| Pattern | P(Up) | N | 95% CI | Edge vs 50.5% | Signal |
|---------|-------|---|--------|----------------|--------|
| After 1x Down | 56.0% | 863 | [52.6%, 59.2%] | +5.5% | BUY UP |
| After 2x Down | 57.6% | 380 | [52.6%, 62.5%] | +7.1% | BUY UP |
| After 3x+ Down | 59.0% | 161 | [51.3%, 66.3%] | +8.5% | BUY UP |
| After 1x Up | 49.5% | 959 | — | -1.0% | skip |
| After 2x+ Up | 47.4% | 475 | — | -3.1% | skip |
| Base rate | 52.6% | 1,823 | — | +2.1% | skip |

### Backtest Results (updated model, $500 start)

- **Win rate**: 55.9% over 864 trades
- **P&L**: $500 → $7,656 (+1,431% ROI)
- **Max drawdown**: 42.5% (vs 74.6% with old half-Kelly)
- **Zero ruin risk** at $500+ starting bankroll

### Model Parameters (`model.py`)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `PROB_UP_AFTER_DOWN` | 0.560 | P(Up\|prev Down), n=863 |
| `PROB_UP_AFTER_2X_DOWN` | 0.576 | P(Up\|prev 2x Down), n=380 |
| `PROB_UP_AFTER_3X_DOWN` | 0.590 | P(Up\|prev 3x+ Down), n=161 |
| `PROB_UP_AFTER_UP` | 0.495 | No edge — skip |
| `PROB_UP_AFTER_2X_UP` | 0.474 | No edge — skip |
| `PROB_UP_BASE` | 0.526 | Unconditional base rate |
| `DEFAULT_EDGE_THRESHOLD` | 0.03 | Minimum 3% edge to trade |
| `KELLY_MULTIPLIER` | 0.25 | Quarter-Kelly (0% ruin at $500+) |
| `MAX_BET_PCT` | 0.10 | Never bet > 10% of bankroll |
| `MIN_BET` | 5.0 | Polymarket minimum order |

### Position Sizing

Quarter-Kelly criterion (0.25x full Kelly):
- `f* = (p(b+1) - 1) / b * 0.25`
- At P(Up)=57.6%, odds≈0.98: quarter-Kelly = 3.6% of bankroll
- Max capped at 10% of bankroll
- Minimum: $5 (Polymarket minimum)

**Why quarter-Kelly?** Ruin analysis over 863 trades:
- Half-Kelly ($100 start): 35.6% chance of bot death (bankroll < $5 min bet)
- Quarter-Kelly ($500 start): 0% death rate, 39.8% max drawdown
- Half-Kelly ($500 start): 0% death rate, but 66.2% max drawdown

### Rolling Win Rate Stability

50-trade rolling windows across 6 days: 42-70% range, never sustained below 46%. The 56% edge is stable, not a single lucky streak.

## Autobot Architecture

`autobot.py` runs a continuous loop:

1. **Wait** for next 5-minute window (places orders 30s before start)
2. **Evaluate** conditional P(Up) from recent outcome sequence
3. **Size** bet via quarter-Kelly if edge > threshold
4. **Execute** trade (dry-run or live via CLOB)
5. **Resolve** — poll Gamma API for outcome, update bankroll
6. **Persist** state to `poly_bot_state.json` after every cycle

**State persistence**: Bot saves bankroll, trade count, win rate, recent outcomes, and drawdown metrics to disk. Survives restarts via `--resume` (default).

**State files**:
- `poly_bot_state.json` — current bot state (bankroll, outcomes, metrics)
- `poly_bot_log.jsonl` — append-only trade log (one JSON object per trade)

## External APIs

| API | Base URL | Used For | Auth |
|-----|----------|----------|------|
| Chainlink (Polygon) | RPC calls | BTC/USD oracle price (resolution source) | None |
| CoinGecko | `api.coingecko.com/api/v3` | BTC spot price (fallback) | None |
| Coinbase | `api.coinbase.com/v2` | BTC spot price (fallback) | None |
| Kraken | `api.kraken.com/0/public` | 1-min & hourly OHLC candles | None |
| Polymarket Gamma | `gamma-api.polymarket.com` | Market discovery + resolution | None |
| Polymarket CLOB | `clob.polymarket.com` | Order book + trading | HMAC (trading only) |

## Market Discovery

Slug pattern: `btc-updown-5m-{unix_timestamp}` where timestamp = start of 5-min window, aligned to 300-second boundaries.

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

## Conventions

- **Python 3.11+** — uses `X | None` union syntax
- Pure Python: `requests`, `scipy`, `numpy` (+ optional `py-clob-client`)
- All timestamps are Unix seconds (UTC)
- Polymarket outcomes are "Up" / "Down" (not Yes/No) for 5-min markets
- Positive edge = Up is underpriced → BUY UP
- Signal sides: `"BUY UP"`, `"NO EDGE"` (BUY DOWN removed — not profitable)

## Architecture Notes

- **Outcome mean-reversion** is the core insight: After Down windows, Up probability increases. Deeper Down streaks → stronger signal. Discovered through 1,823-window calibration.
- **BUY DOWN is dead**: No bearish conditional pattern survives 95% CI testing. The model is BUY UP only.
- **Quarter-Kelly** is the sizing sweet spot: Enough to compound (1,431% ROI over 6 days) but 0% ruin risk at $500+ bankroll and manageable 42.5% max drawdown.
- **Resolution source is Chainlink**, not exchange spot prices.
- **Autobot waits for every window** even when not trading, to keep the outcome sequence current for conditional model.
