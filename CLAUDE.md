# CLAUDE.md — Poly

Polymarket BTC mispricing scanner. Compares Polymarket's crowd-priced Bitcoin up/down markets against a quantitative fair-value model to find profitable edges.

## Project Structure

```
Poly/
├── CLAUDE.md              # This file
├── requirements.txt       # Python dependencies
└── poly/                  # Main package
    ├── __init__.py
    ├── main.py            # CLI entry point — scan & display signals
    ├── btc.py             # BTC spot price (CoinGecko/Coinbase) & realized volatility (Kraken OHLC)
    ├── polymarket.py      # Polymarket market discovery via slug-probing (Gamma + CLOB APIs)
    └── model.py           # Fair-value pricing (log-normal / binary option math) & edge detection
```

## How It Works

1. **Fetch BTC data** — current spot price and annualized realized volatility from hourly closes
2. **Discover markets** — generate slug patterns for known Polymarket BTC strike markets, probe each via Gamma API in parallel
3. **Price each market** — calculate `P(BTC > strike at expiry)` using the log-normal model:
   ```
   P = Φ(d₂)  where  d₂ = [ln(S/K) - (σ²/2)·τ] / (σ·√τ)
   ```
4. **Detect edge** — compare model probability to market's implied probability. Flag when `|edge| > threshold`

## Key Commands

```bash
pip install -r requirements.txt          # Install dependencies
python -m poly.main                      # Run scanner (default: 3% edge threshold)
python -m poly.main -e 0.02              # Lower edge threshold to 2%
python -m poly.main -w 72                # Use 72h volatility window (default: 168h)
python -m poly.main -e 0.05 -w 48        # Combine options
```

## External APIs (no auth required)

| API | Base URL | Used For |
|-----|----------|----------|
| CoinGecko | `api.coingecko.com/api/v3` | BTC spot price (primary) |
| Coinbase | `api.coinbase.com/v2` | BTC spot price (fallback) |
| Kraken | `api.kraken.com/0/public` | Hourly OHLC candles for volatility |
| Polymarket Gamma | `gamma-api.polymarket.com` | Market discovery & metadata |
| Polymarket CLOB | `clob.polymarket.com` | Order book & token prices |

**Important**: Binance API is geo-blocked in some environments (returns 451). That's why we use Kraken for OHLC data.

**Important**: Gamma API search/filter params (`tag`, `slug_contains`, `title_contains`) are unreliable — they return unrelated results. Discovery works by **generating exact slugs** and probing each directly.

## Conventions

- **Python 3.11+** — uses `X | None` union syntax
- Pure Python, no frameworks — just `requests`, `scipy`, `numpy`
- All prices are floats; all timestamps are UTC `datetime` objects
- Polymarket markets use `condition_id` (hex) as the primary key
- Edge = `model_probability - market_probability` (positive = YES is underpriced)
- Default edge threshold is 3% — accounts for spread, fees, and model noise

## Architecture Notes

- **No trading/execution** — this is signal generation only. Placing trades requires Polymarket wallet integration (separate concern).
- **Slug-based discovery** is necessary because the Gamma API's filtering is broken. Slug patterns are generated for `bitcoin-above-{N}k-on-{month}-{day}` and `bitcoin-up-or-down-{month}-{day}-{time}`. The strike list in `polymarket.py:BTC_STRIKES_K` may need updating if Polymarket adds new strike levels.
- **Volatility model** uses realized vol from hourly log returns, annualized via `σ_annual = σ_hourly × √8760`. The 168h (7-day) default window balances recency with stability.
- **Drift is set to zero** — for sub-week timeframes, BTC's expected return is negligible vs. its volatility, so the risk-neutral and real-world probabilities are nearly identical.
