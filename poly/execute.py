"""Execution module for placing trades on Polymarket's CLOB.

Uses the py-clob-client package for authenticated order placement.
Requires a Polygon wallet private key and USDC balance.

Supports:
  - Maker (post-only) limit orders — $0 fee, eligible for 20% rebate
  - Taker (FOK) market orders — 1.56% fee at p=0.50
  - Sell orders — for cash-out / stop-loss during live windows
  - Fill verification — poll order status to confirm maker fills

Setup:
  pip install py-clob-client
  export POLY_PRIVATE_KEY="0x..."
  export POLY_FUNDER="0x..."  (optional, for proxy wallets)
"""

import os
import time
from dataclasses import dataclass

from poly.model import Signal

# Lazy import — py-clob-client is optional (only needed for live trading)
_client = None


@dataclass
class TradeResult:
    """Result of a trade attempt."""

    success: bool
    order_id: str | None
    side: str
    token_id: str
    price: float
    size: float
    error: str | None


@dataclass
class OrderStatus:
    """Status of a resting order from the CLOB.

    Fields from GET /data/order/<order_hash>:
      - size_matched: shares filled so far
      - original_size: total shares requested
      - status: CLOB order status string
    """

    order_id: str
    status: str  # e.g. "live", "matched", "canceled"
    size_matched: float
    original_size: float
    price: float
    side: str
    asset_id: str

    @property
    def fill_fraction(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return self.size_matched / self.original_size

    @property
    def is_fully_filled(self) -> bool:
        return self.size_matched >= self.original_size * 0.99  # 1% tolerance

    @property
    def is_live(self) -> bool:
        return self.status.lower() in ("live", "open")

    @property
    def unfilled_size(self) -> float:
        return max(0.0, self.original_size - self.size_matched)


def _get_client():
    """Lazily initialize the CLOB client."""
    global _client
    if _client is not None:
        return _client

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        raise RuntimeError(
            "py-clob-client not installed. Run: pip install py-clob-client"
        )

    private_key = os.environ.get("POLY_PRIVATE_KEY")
    if not private_key:
        raise RuntimeError(
            "POLY_PRIVATE_KEY environment variable not set. "
            "Export your Polygon wallet private key."
        )

    funder = os.environ.get("POLY_FUNDER")
    sig_type = 1 if funder else 0  # 0=EOA, 1=proxy

    _client = ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=137,  # Polygon mainnet
        signature_type=sig_type,
        funder=funder,
    )
    _client.set_api_creds(_client.create_or_derive_api_creds())
    return _client


def place_maker_order(
    token_id: str,
    price: float,
    size: float,
    side: str = "BUY",
) -> TradeResult:
    """Place a maker (post-only) limit order. $0 fee + rebate eligible.

    GTC order that rests on the book. If it would cross the spread
    (immediately fill), it is rejected — guaranteeing maker status.

    Args:
        token_id: CLOB token ID for the outcome.
        price: Limit price (e.g. 0.50 for Up at bid).
        size: Number of shares.
        side: "BUY" or "SELL".
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    client = _get_client()
    clob_side = BUY if side == "BUY" else SELL

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=clob_side,
        )
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, OrderType.GTC)

        return TradeResult(
            success=True,
            order_id=response.get("orderId"),
            side=side,
            token_id=token_id,
            price=price,
            size=size,
            error=None,
        )
    except Exception as e:
        return TradeResult(
            success=False, order_id=None, side=side,
            token_id=token_id, price=price, size=size,
            error=str(e),
        )


def place_limit_order(
    signal: Signal,
    market,  # FiveMinMarket
    size: float,
    price: float | None = None,
) -> TradeResult:
    """Place a limit order based on a signal.

    Args:
        signal: The trading signal (determines side and token).
        market: The FiveMinMarket (has token IDs).
        size: Number of shares to buy.
        price: Limit price. If None, uses the current best ask.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    client = _get_client()

    if signal.side == "BUY UP":
        token_id = market.up_token_id
        default_price = signal.best_ask if signal.best_ask > 0 else signal.market_prob_up
    elif signal.side == "BUY DOWN":
        token_id = market.down_token_id
        default_price = 1 - signal.best_bid if signal.best_bid > 0 else 1 - signal.market_prob_up
    else:
        return TradeResult(
            success=False, order_id=None, side=signal.side,
            token_id="", price=0, size=0,
            error="No actionable signal",
        )

    order_price = price if price is not None else default_price

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=order_price,
            size=size,
            side=BUY,
        )
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, OrderType.GTC)

        return TradeResult(
            success=True,
            order_id=response.get("orderId"),
            side=signal.side,
            token_id=token_id,
            price=order_price,
            size=size,
            error=None,
        )
    except Exception as e:
        return TradeResult(
            success=False, order_id=None, side=signal.side,
            token_id=token_id, price=order_price, size=size,
            error=str(e),
        )


def place_market_order(
    signal: Signal,
    market,  # FiveMinMarket
    amount_usd: float,
) -> TradeResult:
    """Place a fill-or-kill market order (taker, pays 1.56% fee).

    Args:
        signal: The trading signal.
        market: The FiveMinMarket.
        amount_usd: Total USD to spend.
    """
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    client = _get_client()

    if signal.side == "BUY UP":
        token_id = market.up_token_id
    elif signal.side == "BUY DOWN":
        token_id = market.down_token_id
    else:
        return TradeResult(
            success=False, order_id=None, side=signal.side,
            token_id="", price=0, size=0,
            error="No actionable signal",
        )

    try:
        market_order = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=BUY,
        )
        signed_order = client.create_market_order(market_order)
        response = client.post_order(signed_order, OrderType.FOK)

        return TradeResult(
            success=True,
            order_id=response.get("orderId"),
            side=signal.side,
            token_id=token_id,
            price=0,  # market order, price determined by book
            size=amount_usd,
            error=None,
        )
    except Exception as e:
        return TradeResult(
            success=False, order_id=None, side=signal.side,
            token_id=token_id, price=0, size=amount_usd,
            error=str(e),
        )


def place_taker_buy(
    token_id: str,
    amount_usd: float,
) -> TradeResult:
    """Place a fill-or-kill taker buy for a token.

    Simple taker buy that takes a token_id directly — used for
    dip-buying and arb during live windows where we need immediate fills.

    At low prices the fee is negligible: ~0.16% at 22c vs 1.56% at 50c.

    Args:
        token_id: CLOB token ID for the outcome to buy.
        amount_usd: Total USD to spend.
    """
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    client = _get_client()

    try:
        market_order = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=BUY,
        )
        signed_order = client.create_market_order(market_order)
        response = client.post_order(signed_order, OrderType.FOK)

        return TradeResult(
            success=True,
            order_id=response.get("orderId"),
            side="BUY",
            token_id=token_id,
            price=0,  # market order — price determined by book
            size=amount_usd,
            error=None,
        )
    except Exception as e:
        return TradeResult(
            success=False, order_id=None, side="BUY",
            token_id=token_id, price=0, size=amount_usd,
            error=str(e),
        )


def sell_shares(
    token_id: str,
    size: float,
    price: float | None = None,
    as_maker: bool = True,
) -> TradeResult:
    """Sell shares for cash-out or stop-loss.

    Args:
        token_id: CLOB token ID of shares to sell.
        size: Number of shares to sell.
        price: Limit price. If None, sells at market (FOK).
        as_maker: If True, place as resting limit order ($0 fee).
                  If False, sell as FOK market order (pays ~1.56% fee).
    """
    client = _get_client()

    if price is not None and as_maker:
        return place_maker_order(token_id, price, size, side="SELL")

    # Market sell (taker)
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL

    try:
        market_order = MarketOrderArgs(
            token_id=token_id,
            amount=size,
            side=SELL,
        )
        signed_order = client.create_market_order(market_order)
        response = client.post_order(signed_order, OrderType.FOK)

        return TradeResult(
            success=True,
            order_id=response.get("orderId"),
            side="SELL",
            token_id=token_id,
            price=price or 0,
            size=size,
            error=None,
        )
    except Exception as e:
        return TradeResult(
            success=False, order_id=None, side="SELL",
            token_id=token_id, price=price or 0, size=size,
            error=str(e),
        )


def cancel_order(order_id: str) -> bool:
    """Cancel a resting order by ID. Returns True on success."""
    client = _get_client()
    try:
        client.cancel(order_id)
        return True
    except Exception:
        return False


def cancel_all_orders() -> bool:
    """Cancel all resting orders. Returns True on success."""
    client = _get_client()
    try:
        client.cancel_all()
        return True
    except Exception:
        return False


def get_order_status(order_id: str) -> OrderStatus | None:
    """Fetch current status of an order including fill progress.

    Uses GET /data/order/<order_hash> which returns:
      - size_matched: how many shares have been filled
      - original_size: total shares requested
      - status: order state (live, matched, canceled, etc.)
    """
    client = _get_client()
    try:
        order = client.get_order(order_id)
        return OrderStatus(
            order_id=order_id,
            status=str(order.get("status", "unknown")),
            size_matched=float(order.get("size_matched", 0)),
            original_size=float(order.get("original_size", 0)),
            price=float(order.get("price", 0)),
            side=str(order.get("side", "")),
            asset_id=str(order.get("asset_id", "")),
        )
    except Exception:
        return None


def wait_for_fill(
    order_id: str,
    timeout: float = 30.0,
    poll_interval: float = 2.0,
    cancel_on_timeout: bool = True,
) -> OrderStatus | None:
    """Poll an order until it fills, times out, or is canceled.

    Args:
        order_id: The order ID returned from place_maker_order.
        timeout: Max seconds to wait for fill.
        poll_interval: Seconds between status checks.
        cancel_on_timeout: If True, cancel the order when timeout is reached.

    Returns:
        Final OrderStatus, or None if order couldn't be fetched.
        Check .is_fully_filled and .size_matched on the result.
    """
    deadline = time.time() + timeout
    last_status = None

    while time.time() < deadline:
        status = get_order_status(order_id)
        if status is None:
            time.sleep(poll_interval)
            continue

        last_status = status

        # Fully filled
        if status.is_fully_filled:
            return status

        # Order was canceled or rejected externally
        if status.status.lower() in ("canceled", "cancelled", "rejected"):
            return status

        time.sleep(poll_interval)

    # Timeout reached — cancel unfilled portion if requested
    if cancel_on_timeout and last_status and last_status.is_live:
        cancel_order(order_id)
        # Re-fetch to get final state after cancellation
        final = get_order_status(order_id)
        if final:
            return final

    return last_status


def get_trades_for_market(
    market_id: str,
    after_ts: int | None = None,
) -> list[dict]:
    """Fetch executed trades for a market, optionally after a timestamp.

    Uses GET /data/trades?market=<condition_id>&after=<ts>.
    Useful for post-trade audit trail — confirms actual fills with
    prices, sizes, and fee rates.
    """
    client = _get_client()
    try:
        params = {"market": market_id}
        if after_ts:
            params["after"] = after_ts
        trades = client.get_trades(params)
        return trades if isinstance(trades, list) else []
    except Exception:
        return []


def calculate_bet_size(
    bankroll: float,
    kelly_frac: float,
    max_bet: float = 100.0,
    min_bet: float = 5.0,
) -> float:
    """Calculate bet size from Kelly fraction and bankroll.

    Applies practical limits: minimum order size ($5 on Polymarket),
    maximum bet cap, and never more than 10% of bankroll.
    """
    raw_size = bankroll * kelly_frac
    capped = min(raw_size, max_bet, bankroll * 0.10)
    if capped < min_bet:
        return 0.0  # too small to trade
    return round(capped, 2)
