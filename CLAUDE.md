# CLAUDE.md — Poly

Polymarket 5-minute BTC up/down trading system. Uses outcome mean-reversion (BUY UP after Down windows) with graduated conditional probabilities, quarter-Kelly sizing, maker orders ($0 fee), and optional stop-loss cash-out. Includes mid-window dip-buy and dual-side arbitrage modes.

## Project Structure

```
Poly/
├── CLAUDE.md              # This file
├── requirements.txt       # Python deps: requests, scipy, numpy, websockets
├── deploy/                # Deployment helpers
│   ├── setup.sh           # Hetzner/VPS setup script (systemd, venv, .env)
│   ├── poly-bot.service   # systemd unit for autobot
│   ├── poly-dashboard.service  # systemd unit for web dashboard
│   └── env.example        # Environment variable template
└── poly/                  # Main package
    ├── __init__.py
    ├── main.py            # CLI entry — scan, watch, backtest, trade, autobot, status, test-notify, dashboard
    ├── btc.py             # BTC price, 1-min momentum, realized volatility (Kraken/CoinGecko)
    ├── polymarket.py      # 5-min market discovery, CLOB book, live prices, price history
    ├── model.py           # Conditional mean-reversion model: P(Up|prev outcomes), Kelly sizing, fees, dip/arb params
    ├── backtest.py        # Backtesting engine: real prices, fill-rate simulation
    ├── execute.py         # Order execution: maker/taker, taker buy (dip/arb), fill verification, sell, cancel
    ├── autobot.py         # Automated bot: default/dip/arb modes, live book, fill verification, WS stop-loss, Telegram
    ├── ws.py              # WebSocket client: market price streaming, user fill events
    ├── notify.py          # Telegram notifications: trade alerts, outcomes, dip/arb alerts, command handler, daily summary
    └── dashboard.py       # Web dashboard: equity curve, trade table, stats (Flask/built-in)
```

## How It Works

**Target**: Polymarket's 5-minute BTC up/down markets (`btc-updown-5m-{unix_ts}`).
Each market asks: "Will BTC go up or down in this 5-minute window?" Resolves via Chainlink BTC/USD feed. "Up" wins if `end_price >= start_price`.

**Markets launched**: February 12, 2026 (first market at 00:35 UTC).

**Three Trading Modes**:

### Default Mode (pre-window conditional mean-reversion)
1. **Discovery**: Generate `btc-updown-5m-{timestamp}` slugs and probe Gamma API
2. **Outcomes**: Fetch recent resolved window outcomes (Up/Down sequence)
3. **Model**: Conditional P(Up) based on previous outcome streaks (mean-reversion)
4. **Signal**: Compare model P(Up) to buy price → flag when edge > 3%
5. **Sizing**: Quarter-Kelly criterion with 10% bankroll cap, fee-aware odds
6. **Execution**: Maker limit orders at bid ($0 fee) or taker at ask (1.56% fee)
7. **Cash-Out**: Optional stop-loss sells during live window if Up price < 30c

### Dip-Buy Mode (`--dip`)
1. **Wait** for live window to start
2. **Monitor** Up token price via WebSocket during first 4 minutes
3. **Buy** at graduated levels when Up crashes (35c → 1.5%, 25c → 2.0%, 15c → 2.5% of bankroll)
4. **Hold** all shares to resolution — at low prices, taker fee is negligible (~0.16% at 22c)
5. Max 6% of bankroll per window

### Arb Mode (`--arb`)
1. **Wait** for live window to start
2. **Monitor** both Up + Down prices during first 4 minutes
3. **Buy both sides** when combined price < 88c (guaranteed 12%+ profit)
4. **Hold** to resolution — one side pays $1, guaranteed profit = $1 - combined cost
5. Max 8% of bankroll per window

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
python -m poly.main backtest                       # Last 6 hours, as taker
python -m poly.main backtest --maker               # As maker ($0 fee)
python -m poly.main backtest -H 152 -b 500         # Full history since launch
python -m poly.main backtest -H 152 -b 500 --maker # Full history, maker pricing
python -m poly.main backtest --maker --fill-rate 0.5   # 50% maker fill rate
python -m poly.main backtest --maker --real-prices     # Use actual CLOB prices

# Trade — single-shot trading
python -m poly.main trade                          # Dry run
python -m poly.main trade --live                   # REAL orders

# Autobot — automated continuous trading
python -m poly.main autobot                        # Dry run, maker orders (default mode)
python -m poly.main autobot --taker                # Dry run, taker orders
python -m poly.main autobot --live --maker         # REAL, maker orders ($0 fee)
python -m poly.main autobot --live --stoploss      # REAL, with stop-loss monitoring
python -m poly.main autobot --dip                  # Dry run, dip-buy mode (mid-window)
python -m poly.main autobot --arb                  # Dry run, dual-side arbitrage mode
python -m poly.main autobot --live --dip           # REAL, dip-buy mode
python -m poly.main autobot --live --arb           # REAL, arbitrage mode
python -m poly.main autobot -b 1000               # $1000 bankroll
python -m poly.main autobot -k 0.5                # Half-Kelly (more aggressive)
python -m poly.main autobot --fresh                # Ignore saved state, start fresh
python -m poly.autobot                             # Direct module execution

# Status — check bot state and recent trades from terminal
python -m poly.main status                         # Read state file + trade log

# Test notifications — send test Telegram messages
python -m poly.main test-notify                    # All notification types (requires Telegram config)

# Dashboard — web UI for monitoring
python -m poly.main dashboard                      # http://0.0.0.0:8080
python -m poly.main dashboard --port 9090          # Custom port
python -m poly.dashboard                           # Direct module execution
```

## Polymarket Fee Model

5-minute crypto markets have **taker-only fees** (since launch Feb 12, 2026). Maker orders are free.

Source: `docs.polymarket.com/developers/market-makers/maker-rebates-program`

### Fee Formula

```
fee = C × p × feeRate × (p × (1-p))^exponent
```
where C=shares, p=price. Fee collected as shares on buys, USDC on sells.

| Market Type | feeRate | Exponent | Max Effective | Maker Fee |
|-------------|---------|----------|---------------|-----------|
| **5-min & 15-min crypto** | **0.25** | **2** | **1.56% at p=0.50** | **$0** |
| Sports (NCAAB, Serie A) | 0.0175 | 1 | 0.44% at p=0.50 | $0 |

**Fee at low prices** (relevant for dip-buy mode): The squared exponent makes fees negligible at low prices — ~0.16% at 22c vs 1.56% at 50c. This makes taker buys at crash prices nearly free.

### Order Types and Fees

| Order Type | Fee | Usage |
|------------|-----|-------|
| **Maker (GTC at bid)** | **$0 + 20% rebate pool** | **Recommended for default mode** |
| Taker (FOK at ask) | 1.56% at p=0.50 | Guaranteed fill but costly |
| Taker (FOK at low price) | ~0.16% at p=0.22 | Dip-buy / arb (nearly free) |
| Post-only | $0 (rejected if crosses) | Guaranteed maker status |

### Market Structure (CLOB Order Book)

Pre-window Up token book (typical):
- **Best bid: $0.50** (~930 shares), **Best ask: $0.52** (~200 shares)
- 51 bid levels ($0.01-$0.51), 48 ask levels ($0.52-$0.99)
- Books persist into live window (`clearBookOnStart: false`)

During live window:
- Massive volatility: 50c+ swings in 2 minutes
- Price snaps to 99c/1c near resolution (final 30s)
- Average volume: $123K per window

### Per-Trade Economics: MAKER vs TAKER

| Component | Maker | Taker |
|-----------|-------|-------|
| Buy price | **$0.500 (bid)** | $0.510 (ask) |
| Fee | **$0** | 1.56% |
| Net odds on win | **1.0000** | 0.9302 |
| EV at 56% WR | **+12.0%** | +6.0% |
| **EV improvement** | **+100% vs taker** | baseline |

| Signal | EV/trade (maker) | EV/trade (taker) |
|--------|------------------|------------------|
| After 1x Down (56.0%) | **+12.0%** | +6.0% |
| After 2x Down (57.6%) | **+15.2%** | +9.0% |
| After 3x Down (59.0%) | **+18.0%** | +11.7% |

**Bottom line**: Maker orders DOUBLE expected value by eliminating the 1.56% fee and buying 1c cheaper.

### Monte Carlo Results: $500 start, 864 trades

| Strategy | Median Final | Avg MaxDD |
|----------|-------------|-----------|
| Taker, hold to resolution | **$1,046** | 29.5% |
| **Maker, hold to resolution** | **$7,234** | 38.6% |
| Maker + stop-loss (1/8 Kelly) | **$345,466** | 20.6% |

## Cash-Out / Early Exit Strategy

**Key discovery**: You can sell shares at any time before market resolution. This enables:

1. **Stop-loss**: Sell Up shares if price drops below 30c during live window
   - Limits loss from -100% to ~-40% per trade
   - Dramatically improves EV when combined with maker orders
2. **Take-profit**: Sell winning positions before resolution (not currently implemented)
   - Reduces variance but also reduces EV vs holding to $1 payout
3. **Cash-out fee**: Taker sell costs 1.56% (same formula), maker sell costs $0

### Stop-Loss Parameters

| Parameter | Value |
|-----------|-------|
| `STOP_LOSS_PRICE` | 0.30 (sell if Up < 30c) |
| `STOPLOSS_POLL` | 5s (check interval) |
| Estimated trigger rate | ~50% of losing trades |
| Loss reduction | -100% → -40% on stopped trades |

## Model Design

### Core Strategy: Outcome Mean-Reversion

After a Down window, the next window has a higher-than-market probability of being Up (mean-reversion). This effect intensifies with consecutive Downs.

**BUY DOWN does NOT work** — no Down signal survives 95% confidence interval testing.

### Calibration Data

6-day backtest over 1,823 resolved windows (Feb 12-18 2026, 861 actionable trades):

| Pattern | P(Up) | N | 95% CI | Edge vs 50.0% bid | Signal |
|---------|-------|---|--------|-------------------|--------|
| After 1x Down | 56.0% | 863 | [52.6%, 59.2%] | +6.0% | BUY UP |
| After 2x Down | 57.6% | 380 | [52.6%, 62.5%] | +7.6% | BUY UP |
| After 3x+ Down | 59.0% | 161 | [51.3%, 66.3%] | +9.0% | BUY UP |
| After 1x Up | 49.5% | 959 | — | -0.5% | skip |
| After 2x+ Up | 47.4% | 475 | — | -2.6% | skip |
| Base rate | 52.6% | 1,823 | — | +2.6% | skip |

### Model Parameters (`model.py`)

#### Default Mode — Conditional Mean-Reversion

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `PROB_UP_AFTER_DOWN` | 0.560 | P(Up\|prev Down), n=863 |
| `PROB_UP_AFTER_2X_DOWN` | 0.576 | P(Up\|prev 2x Down), n=380 |
| `PROB_UP_AFTER_3X_DOWN` | 0.590 | P(Up\|prev 3x+ Down), n=161 |
| `PROB_UP_AFTER_UP` | 0.495 | No edge — skip |
| `PROB_UP_AFTER_2X_UP` | 0.474 | No edge — skip |
| `PROB_UP_BASE` | 0.526 | Unconditional base rate |
| `DEFAULT_EDGE_THRESHOLD` | 0.03 | Minimum 3% edge to trade |
| `IN_PROGRESS_EDGE_THRESHOLD` | 0.05 | 5% edge for in-progress markets |
| `KELLY_MULTIPLIER` | 0.25 | Quarter-Kelly (0% ruin at $500+) |
| `MAX_BET_PCT` | 0.10 | Never bet > 10% of bankroll |
| `MIN_BET` | 5.0 | Polymarket minimum order |
| `FEE_RATE` | 0.25 | 5-min crypto fee rate |
| `FEE_EXPONENT` | 2 | Squared curve for crypto markets |
| `DEFAULT_BUY_PRICE` | 0.510 | Typical ask price (taker) |
| `MAKER_BUY_PRICE` | 0.500 | Typical bid price (maker, $0 fee) |
| `STOP_LOSS_PRICE` | 0.30 | Sell if Up token drops below 30c |

#### Dip-Buy Mode Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `DIP_ENTRY_PRICE` | 0.35 | Start buying when Up drops below 35c |
| `DIP_LEVELS` | [(0.35, 0.015), (0.25, 0.020), (0.15, 0.025)] | Graduated buy levels (price, bankroll %) |
| `DIP_MAX_PER_WINDOW` | 0.06 | Max 6% of bankroll total per window |
| `DIP_MONITOR_SECS` | 240 | Monitor first 4 minutes (leave 60s before resolution) |

Dip-buy economics: At 22c, odds are ~3.5:1 and fee is ~0.16%. At 15c, odds are ~5.7:1 and fee is ~0.04%. The squared fee curve makes low-price taker buys nearly free.

#### Dual-Side Arbitrage Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `ARB_MAX_COMBINED` | 0.88 | Buy both sides when combined < 88c (12%+ guaranteed) |
| `ARB_BET_PCT` | 0.04 | 4% of bankroll per arb opportunity |
| `ARB_MAX_PER_WINDOW` | 0.08 | Max 8% total per window |
| `ARB_MONITOR_SECS` | 240 | Monitor first 4 minutes |

Arb mechanics: When Up + Down combined price < $1.00, buying both sides guarantees profit since one side always pays $1 at resolution. Profit = $1.00 - combined_cost per share pair.

### Position Sizing

Quarter-Kelly criterion (0.25x full Kelly) with **fee-adjusted odds**:
- As maker: `net_odds = 1/0.50 - 1 = 1.0` (100% return on win)
- As taker: `net_odds = (1/0.51) * (1 - 0.0156) - 1 = 0.9302`
- At P(Up)=56%, maker: quarter-Kelly = 3.0% of bankroll
- At P(Up)=56%, taker: quarter-Kelly = 2.2% of bankroll

### Rolling Win Rate Stability

50-trade rolling windows across 6 days: 42-70% range, never sustained below 46%. The 56% edge is stable, not a single lucky streak.

## Autobot Architecture

`autobot.py` runs a continuous loop with three trading modes:

### Mode Selection

| Mode | Flag | Strategy | When Orders Placed |
|------|------|----------|-------------------|
| **Default** | (none) or `--maker`/`--taker` | Pre-window conditional mean-reversion | 30s before window starts |
| **Dip-buy** | `--dip` | Buy Up when it crashes mid-window | During live window (first 4 min) |
| **Arb** | `--arb` | Buy both sides when combined < 88c | During live window (first 4 min) |

### Default Mode Cycle

1. **Wait** for next 5-minute window (places orders 30s before start)
2. **Book** — fetch live order book for real bid/ask prices
3. **Evaluate** conditional P(Up) from recent outcome sequence
4. **Size** bet via quarter-Kelly if edge > threshold (using live prices)
5. **Execute** maker limit buy at live bid (or taker FOK at live ask)
6. **Verify** — poll order status for fill confirmation, cancel unfilled on timeout
7. **Monitor** (optional) WebSocket-based stop-loss during live window (REST fallback)
8. **Resolve** — poll Gamma API for outcome, update bankroll using actual fill size
9. **Persist** state to `poly_bot_state.json` after every cycle

### Dip-Buy Mode Cycle

1. **Wait** for live window to start
2. **Monitor** Up token price via WebSocket (first 4 minutes)
3. **Buy** at graduated levels: 35c (1.5%), 25c (2.0%), 15c (2.5%)
4. **Hold** all shares to resolution
5. **Resolve** — update bankroll based on outcome vs total cost

### Arb Mode Cycle

1. **Wait** for live window to start
2. **Monitor** both Up + Down prices via WebSocket (first 4 minutes)
3. **Buy both sides** when combined < 88c (4% of bankroll)
4. **Hold** to resolution — guaranteed profit from the gap
5. **Resolve** — update bankroll (always a win if filled)

### Flags

- `--maker` (default) — use post-only limit orders at bid ($0 fee, 2x EV)
- `--taker` — use FOK market orders at ask (1.56% fee)
- `--stoploss` — monitor CLOB price during live window, sell if Up < 30c
- `--dip` — dip-buy mode: buy Up when it crashes mid-window at graduated levels
- `--arb` — arb mode: buy both sides when combined price < 88c

**State persistence**: Bot saves bankroll, trade count, win rate, recent outcomes, drawdown metrics, and stop-out count to disk. Survives restarts. Legacy state files are forward-compatible.

**State files**:
- `poly_bot_state.json` — current bot state (bankroll, outcomes, metrics)
- `poly_bot_log.jsonl` — append-only trade log (one JSON object per trade)

**Daily summary**: Autobot automatically sends a Telegram daily summary at ~00:00 UTC.

## Execution Module

`execute.py` uses `py-clob-client` for authenticated order placement on Polygon (chain ID 137).

**Setup**:
```bash
pip install py-clob-client
export POLY_PRIVATE_KEY="0x..."      # Polygon wallet private key
export POLY_FUNDER="0x..."           # Optional: proxy wallet address
```

**Order Functions**:
- `place_maker_order(token_id, price, size, side)` — GTC limit, maker ($0 fee)
- `place_market_order(signal, market, amount)` — FOK, taker (1.56% fee)
- `place_taker_buy(token_id, amount_usd)` — FOK taker buy by token ID (used for dip-buy and arb)
- `place_limit_order(signal, market, size, price)` — GTC limit
- `sell_shares(token_id, size, price, as_maker)` — Sell for cash-out / stop-loss
- `cancel_order(order_id)` — Cancel a resting order
- `cancel_all_orders()` — Cancel all resting orders

**Fill Verification**:
- `get_order_status(order_id)` → `OrderStatus` with `size_matched`, `original_size`, `fill_fraction`
- `wait_for_fill(order_id, timeout, poll_interval, cancel_on_timeout)` → polls until filled or timeout
- `get_trades_for_market(market_id, after_ts)` → executed trade audit trail

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

## Conventions

- **Python 3.11+** — uses `X | None` union syntax
- Pure Python: `requests`, `scipy`, `numpy` (+ optional `flask`, `py-clob-client`)
- All timestamps are Unix seconds (UTC)
- Polymarket outcomes are "Up" / "Down" (not Yes/No) for 5-min markets
- Positive edge = Up is underpriced → BUY UP
- Signal sides: `"BUY UP"`, `"NO EDGE"` (BUY DOWN removed — not profitable)

## Monitoring & Notifications

Four channels for observing the bot:

### 1. Telegram Alerts (`notify.py`)

Push notifications to your phone on every trade event.

**Setup**:
```bash
# 1. Create bot via @BotFather → get token
# 2. Get chat ID: curl https://api.telegram.org/bot<TOKEN>/getUpdates
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_CHAT_ID="987654321"
```

**Events notified**:
| Event | When | Content |
|-------|------|---------|
| Startup | Bot starts | Mode, bankroll, settings |
| Trade placed | Order submitted | Side, size, price, edge, streak |
| Fill update | Maker fill verified | Shares filled, fill % |
| Dip buy | Up crashes mid-window | Price, level, odds, fee %, total spent |
| Arb buy | Combined < 88c | Up/Down prices, combined, gap, guaranteed profit % |
| Outcome | Window resolves | WIN/LOSS, P&L, bankroll, WR |
| Dip/Arb outcome | Dip or arb resolves | Num buys, total cost, P&L |
| Stop-loss | Price < 30c | Exit price, recovery %, P&L |
| Skip | Window skipped | Reason, bankroll |
| Daily summary | ~00:00 UTC (auto) | 24h trades, WR, P&L, drawdown |
| Error | Exception caught | Error message |
| Shutdown | Bot stops | Final bankroll, stats |

Uses raw `requests` to Telegram Bot API — no extra dependency. All sends are non-blocking (background threads). Gracefully degrades if not configured (no env vars = no notifications).

### 2. Telegram Command Handler (interactive)

The bot starts a background long-polling thread that listens for commands from the owner's Telegram chat. Only responds to the configured `TELEGRAM_CHAT_ID`.

| Command | Response |
|---------|----------|
| `/status` | Bankroll, WR, P&L, drawdown, ROI, recent outcomes |
| `/trades` | Last 10 trades with results and P&L |
| `/today` | Today's stats (trades, WR, P&L, stop-outs) |
| `/daily` | Send daily summary now (on demand) |
| `/ping` | "Pong! Bot is running." |
| `/help` | List all available commands |
| `/start` | Alias for /help |

The command handler starts automatically when the autobot boots (if Telegram is configured) and stops on shutdown. Uses `getUpdates` with long-polling (30s timeout).

### 3. Web Dashboard (`dashboard.py`)

Single-page web UI served on port 8080. Reads `poly_bot_state.json` and `poly_bot_log.jsonl`.

**Features**:
- Equity curve (Chart.js)
- Summary cards: bankroll, P&L, WR, drawdown, streak, today's stats
- Recent outcomes pill display
- Trade table with fill %, edge, P&L
- Auto-refreshes every 60 seconds
- JSON API: `GET /api/status`, `GET /api/trades`

**Dependencies**: `flask` (optional — falls back to Python's built-in `http.server`).

### 4. CLI Status (`python -m poly.main status`)

Terminal command that reads state/log files and prints a summary. Zero dependencies, works over SSH.

Shows: bankroll, P&L, ROI, WR, trades, drawdown, recent outcomes, last 10 trades, today's stats.

## Deployment (Hetzner/VPS)

**Server**: `46.225.27.241` (Hetzner, Ubuntu 24.04)
**GitHub**: `https://github.com/Rn5ho/Poly`
**No local files on dev machine** — all code lives in GitHub. Deploy by pulling from GitHub on the server.

### Deploying code changes

```bash
ssh root@46.225.27.241
cd /opt/poly
git fetch origin <branch-name>
git checkout origin/<branch-name> -- poly/autobot.py  # or whichever files changed
chown poly:poly poly/*.py
systemctl restart poly-bot
```

### Fresh deploy (first time)

```bash
ssh root@46.225.27.241
cd /opt && git clone https://github.com/Rn5ho/Poly.git poly
bash /opt/poly/deploy/setup.sh
nano /opt/poly/.env   # add secrets
systemctl start poly-bot
```

**Systemd services**:
| Service | Command | Port |
|---------|---------|------|
| `poly-bot` | Autobot (maker, dry-run by default, `--fresh -b 100`) | — |
| `poly-dashboard` | Web dashboard | 8080 |

**Current service ExecStart**: `/opt/poly/venv/bin/python -u -m poly.main autobot --maker -b 100 --fresh`

**Logs**: stdout is redirected to files, NOT journalctl. Use `tail -f` to follow:
- Bot: `tail -f /var/log/poly/bot.log`
- Dashboard: `tail -f /var/log/poly/dashboard.log`
- `journalctl -u poly-bot` only shows systemd lifecycle messages (Started/Stopped), not Python output.

**Important**: Both service files set `Environment=PYTHONUNBUFFERED=1` so Python output flushes immediately to the log files. Without this, Python buffers stdout and the log files appear empty.

**To switch to live trading**: Edit `/etc/systemd/system/poly-bot.service`, change `ExecStart` to include `--live`, then `systemctl daemon-reload && systemctl restart poly-bot`.

**To switch modes**: Add `--dip` or `--arb` to `ExecStart` in the service file, then reload and restart.

## Architecture Notes

- **Outcome mean-reversion** is the core insight: After Down windows, Up probability increases. Deeper Down streaks → stronger signal.
- **BUY DOWN is dead**: No bearish conditional pattern survives 95% CI testing.
- **Three trading modes**: Default (pre-window conditional), dip-buy (mid-window crash buying), arb (dual-side guaranteed profit). Modes are mutually exclusive per bot instance.
- **Maker orders are the #1 profitability lever**: $0 fee + 1c better price = 2x EV vs taker. Default mode.
- **Fill verification is critical**: Maker orders queue behind existing liquidity. The bot polls `GET /data/order/<id>` to confirm `size_matched` before counting a trade. Unfilled orders are canceled before window start.
- **Live book prices** replace hardcoded bid/ask. The bot fetches the CLOB order book before every trade to use real bid/ask for edge calculation and sizing.
- **Cash-out (sell early)** is the #2 lever: Stop-loss at 30c limits losing trades from -100% to ~-40%.
- **WebSocket stop-loss** uses the market channel for sub-second price updates (falls back to REST polling if unavailable).
- **Dip-buy uses taker orders** at low prices where fees are negligible due to the squared fee curve. `place_taker_buy()` takes a token_id directly for immediate fills.
- **Arb mode** buys both Up and Down tokens when their combined price is below $1.00. Guaranteed profit regardless of outcome since one side always resolves to $1.
- **Quarter-Kelly** is the sizing sweet spot: 0% ruin risk at $500+ bankroll.
- **Fees are significant but survivable**: 1.56% taker fee eats ~50% of EV. Maker eliminates it. At low prices (<30c), taker fee drops below 0.5%.
- **Resolution source is Chainlink**, not exchange spot prices.
- **Autobot saves state immediately on startup** (including `--fresh`), so the dashboard shows correct bankroll before the first trade completes.
- **Autobot waits for every window** even when not trading, to keep outcome sequence current.
- **Daily summary** is sent automatically at ~00:00 UTC, or on-demand via `/daily` Telegram command.
- **Telegram command handler** runs as a background thread, long-polling for interactive commands from the bot owner. Starts/stops with the autobot lifecycle.
- **Backtest honesty**: `--fill-rate` simulates partial maker fills, `--real-prices` uses actual historical CLOB prices instead of assumed constants.
- **Telegram notifications** are non-blocking (background threads) and gracefully degrade if not configured. The bot runs identically with or without Telegram.
- **Web dashboard** reads the same JSONL log and state file the bot writes. No coupling — dashboard can be restarted independently. Falls back to Python built-in HTTP server if Flask is not installed.
