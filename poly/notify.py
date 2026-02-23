"""Telegram notification module for the Poly trading bot.

Sends push alerts to Telegram on:
  - Trade placed (side, size, price, edge)
  - Trade resolved (outcome, P&L, bankroll)
  - Stop-loss triggered (exit price, recovery %)
  - Daily summary (WR, P&L, drawdown, trade count)
  - Errors and warnings

Uses raw requests to Telegram Bot API — no extra dependency beyond `requests`.

Setup:
  1. Create a bot via @BotFather on Telegram → get BOT_TOKEN
  2. Send /start to your bot, then get your chat ID via:
     curl https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Export environment variables:
     export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
     export TELEGRAM_CHAT_ID="987654321"
"""

import json
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Throttle: max 1 message per second (Telegram rate limit is 30/sec but be polite)
_last_send = 0.0
_lock = threading.Lock()

LOG_FILE = Path("poly_bot_log.jsonl")


def _get_config() -> tuple[str, str] | None:
    """Get Telegram bot token and chat ID from environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return None
    return token, chat_id


def _send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to Telegram. Returns True on success."""
    global _last_send
    config = _get_config()
    if not config:
        return False

    token, chat_id = config

    with _lock:
        elapsed = time.time() - _last_send
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        try:
            resp = requests.post(
                TELEGRAM_API.format(token=token, method="sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            _last_send = time.time()
            return resp.status_code == 200
        except Exception:
            return False


def _send_async(text: str) -> None:
    """Send a Telegram message in a background thread (non-blocking)."""
    threading.Thread(target=_send, args=(text,), daemon=True).start()


def is_configured() -> bool:
    """Check if Telegram notifications are configured."""
    return _get_config() is not None


def notify_trade_placed(
    window_time: str,
    side: str,
    order_type: str,
    bet_size: float,
    buy_price: float,
    model_prob: float,
    edge: float,
    bankroll: float,
    down_streak: int,
) -> None:
    """Notify when a trade is placed."""
    text = (
        f"📊 <b>TRADE PLACED</b>\n"
        f"Window: {window_time}\n"
        f"Side: {side} ({order_type})\n"
        f"Size: ${bet_size:.2f} @ {buy_price:.3f}\n"
        f"Model: {model_prob:.1%} | Edge: {edge:+.1%}\n"
        f"Down streak: {down_streak}\n"
        f"Bankroll: ${bankroll:,.2f}"
    )
    _send_async(text)


def notify_fill(
    order_id: str | None,
    shares_filled: float,
    shares_requested: float,
    fill_fraction: float,
) -> None:
    """Notify on fill verification result."""
    if fill_fraction >= 0.99:
        text = f"✅ <b>FILLED</b>: {shares_filled:.1f} shares (100%)"
    elif fill_fraction > 0:
        text = (
            f"⚠️ <b>PARTIAL FILL</b>: {shares_filled:.1f}/{shares_requested:.1f} "
            f"shares ({fill_fraction:.0%})"
        )
    else:
        text = f"❌ <b>NO FILL</b> — order canceled"
    _send_async(text)


def notify_outcome(
    window_time: str,
    outcome: str,
    won: bool,
    pnl: float,
    bankroll: float,
    win_rate: float,
    total_trades: int,
    drawdown: float,
    stopped_out: bool = False,
) -> None:
    """Notify when a trade resolves."""
    if stopped_out:
        icon = "🛑"
        label = "STOP-LOSS"
    elif won:
        icon = "🟢"
        label = "WIN"
    else:
        icon = "🔴"
        label = "LOSS"

    text = (
        f"{icon} <b>{label}</b>: {outcome}\n"
        f"Window: {window_time}\n"
        f"P&L: <b>${pnl:+.2f}</b>\n"
        f"Bankroll: ${bankroll:,.2f}\n"
        f"WR: {win_rate:.1%} ({total_trades} trades)\n"
        f"Drawdown: {drawdown:.1%}"
    )
    _send_async(text)


def notify_stoploss(
    window_time: str,
    exit_price: float,
    pnl: float,
    recovery_pct: float,
) -> None:
    """Notify when stop-loss triggers."""
    text = (
        f"🛑 <b>STOP-LOSS TRIGGERED</b>\n"
        f"Window: {window_time}\n"
        f"Exit price: {exit_price:.2f}\n"
        f"P&L: ${pnl:+.2f}\n"
        f"Recovery: {recovery_pct:.0%}"
    )
    _send_async(text)


def notify_dip_buy(
    window_time: str,
    price: float,
    bet_size: float,
    shares: float,
    level: float,
    total_spent: float,
    bankroll: float,
) -> None:
    """Notify when a dip-buy is triggered during a live window."""
    odds = (1.0 / price - 1) if price > 0 else 0
    text = (
        f"🔻 <b>DIP BUY</b>\n"
        f"Window: {window_time}\n"
        f"Up price: {price:.3f} (≤{level:.2f} trigger)\n"
        f"Size: ${bet_size:.2f} → {shares:.1f} shares\n"
        f"Odds: {odds:.1f}:1 | Fee: ~{_dip_fee_pct(price):.2f}%\n"
        f"Window total: ${total_spent:.2f}\n"
        f"Bankroll: ${bankroll:,.2f}"
    )
    _send_async(text)


def _dip_fee_pct(price: float) -> float:
    """Approximate taker fee % at a given price."""
    return 25.0 * (price * (1 - price)) ** 2


def notify_arb_buy(
    window_time: str,
    up_price: float,
    down_price: float,
    up_bet: float,
    down_bet: float,
    guaranteed_profit_pct: float,
    bankroll: float,
) -> None:
    """Notify when a dual-side arb buy is placed."""
    combined = up_price + down_price
    text = (
        f"⚖️ <b>ARB BUY</b>\n"
        f"Window: {window_time}\n"
        f"Up: ${up_bet:.2f} @ {up_price:.3f}\n"
        f"Down: ${down_bet:.2f} @ {down_price:.3f}\n"
        f"Combined: {combined:.3f} (gap: {1-combined:.3f})\n"
        f"Min profit: {guaranteed_profit_pct:.1f}%\n"
        f"Bankroll: ${bankroll:,.2f}"
    )
    _send_async(text)


def notify_dip_outcome(
    window_time: str,
    outcome: str,
    num_buys: int,
    total_cost: float,
    total_pnl: float,
    bankroll: float,
    win_rate: float,
    total_trades: int,
    mode: str = "DIP",
) -> None:
    """Notify resolution for dip-buy or arb trades."""
    won = total_pnl > 0
    icon = "🟢" if won else "🔴"
    label = "PROFIT" if won else "LOSS"
    text = (
        f"{icon} <b>{mode} {label}</b>: {outcome}\n"
        f"Window: {window_time}\n"
        f"Buys: {num_buys} | Cost: ${total_cost:.2f}\n"
        f"P&L: <b>${total_pnl:+.2f}</b>\n"
        f"Bankroll: ${bankroll:,.2f}\n"
        f"WR: {win_rate:.1%} ({total_trades} trades)"
    )
    _send_async(text)


def notify_skip(window_time: str, reason: str, bankroll: float) -> None:
    """Notify when a window is skipped (optional, can be noisy)."""
    text = (
        f"⏭️ Skip: {window_time}\n"
        f"Reason: {reason}\n"
        f"Bank: ${bankroll:,.2f}"
    )
    _send_async(text)


def notify_error(error: str) -> None:
    """Notify on bot errors."""
    text = f"🚨 <b>BOT ERROR</b>\n{error[:500]}"
    _send_async(text)


def notify_startup(
    mode: str,
    order_type: str,
    bankroll: float,
    kelly: float,
    edge_threshold: float,
    stoploss: bool,
) -> None:
    """Notify on bot startup."""
    sl = f"SL: {stoploss}" if stoploss else "No SL"
    text = (
        f"🤖 <b>BOT STARTED</b>\n"
        f"Mode: {mode} | {order_type}\n"
        f"Bankroll: ${bankroll:,.2f}\n"
        f"Kelly: {kelly:.0%} | Edge: {edge_threshold:.0%}\n"
        f"{sl}"
    )
    _send_async(text)


def notify_shutdown(
    bankroll: float,
    total_trades: int,
    win_rate: float,
    total_pnl: float,
    max_drawdown: float,
) -> None:
    """Notify on bot shutdown."""
    text = (
        f"🛑 <b>BOT STOPPED</b>\n"
        f"Bankroll: ${bankroll:,.2f}\n"
        f"Trades: {total_trades} | WR: {win_rate:.1%}\n"
        f"P&L: ${total_pnl:+,.2f}\n"
        f"Max DD: {max_drawdown:.1%}"
    )
    _send(text)  # blocking — want this to send before exit


def send_daily_summary() -> None:
    """Send a daily summary from the trade log and state file.

    Uses state file for current bankroll (avoids cross-session contamination).
    Uses trade log for last-24h trade stats (count, WR, P&L).
    """
    # Get current bankroll from state file (authoritative, session-aware)
    state = _load_state()
    current_bank = state.get("bankroll", 0)
    all_time_trades = state.get("total_trades", 0)

    # Get last-24h trades from log
    cutoff = time.time() - 86400
    trades = []
    if LOG_FILE.exists():
        try:
            for line in LOG_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                trade = json.loads(line)
                if trade.get("ts", 0) >= cutoff:
                    trades.append(trade)
        except Exception:
            pass

    if not trades and not state:
        _send("📊 <b>DAILY SUMMARY</b>\nNo trades in last 24h.")
        return

    if trades:
        wins = sum(1 for t in trades if t.get("won"))
        losses = len(trades) - wins
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        wr = wins / len(trades)
        stopouts = sum(1 for t in trades if t.get("stopped_out"))
        max_dd = max((t.get("drawdown", 0) for t in trades), default=0)
    else:
        wins = losses = 0
        total_pnl = 0.0
        wr = 0.0
        stopouts = 0
        max_dd = state.get("max_drawdown", 0)

    text = (
        f"📊 <b>DAILY SUMMARY</b> ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n\n"
        f"Trades (24h): {len(trades)} ({wins}W / {losses}L)\n"
        f"Win rate: {wr:.1%}\n"
        f"P&L (24h): <b>${total_pnl:+,.2f}</b>\n"
        f"Bankroll: ${current_bank:,.2f}\n"
        f"Max DD: {max_dd:.1%}\n"
        f"Stop-outs: {stopouts}\n"
        f"Total trades: {all_time_trades}"
    )
    _send(text)


# ---------------------------------------------------------------------------
# Telegram Command Handler — interactive commands from phone
# ---------------------------------------------------------------------------

STATE_FILE = Path("poly_bot_state.json")

_cmd_thread: threading.Thread | None = None
_cmd_stop = threading.Event()


def _tg_api(method: str, **params) -> dict | None:
    """Call a Telegram Bot API method."""
    config = _get_config()
    if not config:
        return None
    token, _ = config
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token, method=method),
            json=params,
            timeout=15,
        )
        data = resp.json()
        return data if data.get("ok") else None
    except Exception:
        return None


def _load_state() -> dict:
    """Load bot state from JSON file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _load_trades(last_n: int = 0, hours: float = 0) -> list[dict]:
    """Load trades from the JSONL log."""
    if not LOG_FILE.exists():
        return []
    trades = []
    cutoff = time.time() - (hours * 3600) if hours > 0 else 0
    try:
        for line in LOG_FILE.read_text().strip().split("\n"):
            if not line.strip():
                continue
            t = json.loads(line)
            if cutoff and t.get("ts", 0) < cutoff:
                continue
            trades.append(t)
    except Exception:
        return []
    if last_n > 0:
        return trades[-last_n:]
    return trades


def _cmd_status() -> str:
    """Handle /status command."""
    s = _load_state()
    if not s:
        return "No bot state found."

    bankroll = s.get("bankroll", 0)
    starting = s.get("starting_bankroll", 0)
    trades = s.get("total_trades", 0)
    wins = s.get("total_wins", 0)
    pnl = s.get("total_pnl", 0)
    max_dd = s.get("max_drawdown", 0)
    stopouts = s.get("total_stopouts", 0)
    recent = s.get("recent_outcomes", [])
    mode = "DRY RUN" if s.get("dry_run", True) else "LIVE"
    order_type = "MAKER" if s.get("use_maker") else "TAKER"
    wr = wins / trades if trades > 0 else 0
    roi = ((bankroll - starting) / starting) if starting > 0 else 0

    return (
        f"📊 <b>BOT STATUS</b>\n"
        f"Mode: {mode} ({order_type})\n"
        f"Since: {s.get('started_at', '?')}\n\n"
        f"Bankroll: <b>${bankroll:,.2f}</b>\n"
        f"Starting: ${starting:,.2f}\n"
        f"P&L: ${pnl:+,.2f} ({roi:+.1%} ROI)\n\n"
        f"Trades: {trades} ({wins}W / {trades - wins}L)\n"
        f"Win rate: {wr:.1%}\n"
        f"Max DD: {max_dd:.1%}\n"
        f"Stop-outs: {stopouts}\n\n"
        f"Recent: {' → '.join(recent[-8:]) if recent else 'N/A'}"
    )


def _cmd_trades() -> str:
    """Handle /trades command — last 10 trades."""
    trades = _load_trades(last_n=10)
    if not trades:
        return "No trades logged yet."

    lines = ["📋 <b>LAST 10 TRADES</b>\n"]
    for t in trades:
        ts = t.get("ts", 0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        time_str = dt.strftime("%m/%d %H:%M") if dt else "?"
        result = "✅" if t.get("won") else ("🛑" if t.get("stopped_out") else "❌")
        pnl = t.get("pnl", 0)
        lines.append(
            f"{result} {time_str}  ${t.get('bet', 0):.0f} → "
            f"{'+'if pnl >= 0 else ''}${pnl:.2f}  "
            f"(${t.get('bankroll', 0):,.0f})"
        )
    return "\n".join(lines)


def _cmd_today() -> str:
    """Handle /today command — today's stats."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    trades = [t for t in _load_trades() if t.get("ts", 0) >= today_start]

    if not trades:
        return "📅 <b>TODAY</b>\nNo trades yet today."

    wins = sum(1 for t in trades if t.get("won"))
    pnl = sum(t.get("pnl", 0) for t in trades)
    wr = wins / len(trades) if trades else 0
    stopouts = sum(1 for t in trades if t.get("stopped_out"))

    return (
        f"📅 <b>TODAY</b> ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n\n"
        f"Trades: {len(trades)} ({wins}W / {len(trades) - wins}L)\n"
        f"Win rate: {wr:.1%}\n"
        f"P&L: <b>${pnl:+,.2f}</b>\n"
        f"Stop-outs: {stopouts}"
    )


def _cmd_help() -> str:
    """Handle /help command."""
    return (
        "🤖 <b>WhaleMatic Commands</b>\n\n"
        "/status — Bankroll, WR, P&L, drawdown\n"
        "/trades — Last 10 trades\n"
        "/today — Today's stats\n"
        "/daily — Send daily summary now\n"
        "/ping — Check if bot is alive\n"
        "/help — This message"
    )


_COMMANDS = {
    "/status": _cmd_status,
    "/trades": _cmd_trades,
    "/today": _cmd_today,
    "/daily": lambda: (send_daily_summary(), "Daily summary sent.")[-1],
    "/ping": lambda: "🏓 Pong! Bot is running.",
    "/help": _cmd_help,
    "/start": _cmd_help,
}


def _command_poll_loop() -> None:
    """Background loop: long-poll Telegram for commands, respond inline."""
    config = _get_config()
    if not config:
        return
    _, owner_chat_id = config
    offset = 0

    while not _cmd_stop.is_set():
        try:
            data = _tg_api("getUpdates", offset=offset, timeout=30)
            if not data or not data.get("result"):
                continue

            for update in data["result"]:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                # Only respond to the bot owner
                if chat_id != owner_chat_id:
                    continue

                cmd = text.split()[0].lower() if text else ""
                if cmd in _COMMANDS:
                    reply = _COMMANDS[cmd]()
                    _send(reply)
        except Exception:
            # Don't crash the poller — just retry
            _cmd_stop.wait(5)


def start_command_handler() -> None:
    """Start the Telegram command handler in a background thread."""
    global _cmd_thread
    if not is_configured():
        return
    if _cmd_thread and _cmd_thread.is_alive():
        return
    _cmd_stop.clear()
    _cmd_thread = threading.Thread(target=_command_poll_loop, daemon=True)
    _cmd_thread.start()


def stop_command_handler() -> None:
    """Stop the Telegram command handler."""
    _cmd_stop.set()
