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

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

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
                TELEGRAM_API.format(token=token),
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
    """Send a daily summary from the trade log.

    Reads poly_bot_log.jsonl and summarizes the last 24 hours.
    Call this from a cron job or at a fixed time in the bot loop.
    """
    if not LOG_FILE.exists():
        return

    cutoff = time.time() - 86400
    trades = []
    try:
        for line in LOG_FILE.read_text().strip().split("\n"):
            if not line:
                continue
            trade = json.loads(line)
            if trade.get("ts", 0) >= cutoff:
                trades.append(trade)
    except Exception:
        return

    if not trades:
        _send("📊 <b>DAILY SUMMARY</b>\nNo trades in last 24h.")
        return

    wins = sum(1 for t in trades if t.get("won"))
    losses = len(trades) - wins
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    wr = wins / len(trades) if trades else 0
    stopouts = sum(1 for t in trades if t.get("stopped_out"))
    final_bank = trades[-1].get("bankroll", 0)
    max_dd = max((t.get("drawdown", 0) for t in trades), default=0)

    text = (
        f"📊 <b>DAILY SUMMARY</b> ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n\n"
        f"Trades: {len(trades)} ({wins}W / {losses}L)\n"
        f"Win rate: {wr:.1%}\n"
        f"P&L: <b>${total_pnl:+,.2f}</b>\n"
        f"Bankroll: ${final_bank:,.2f}\n"
        f"Max DD: {max_dd:.1%}\n"
        f"Stop-outs: {stopouts}"
    )
    _send(text)
