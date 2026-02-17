"""Execution module for placing trades on Polymarket's CLOB.

Uses the py-clob-client package for authenticated order placement.
Requires a Polygon wallet private key and USDC balance.

Setup:
  pip install py-clob-client
  export POLY_PRIVATE_KEY="0x..."
  export POLY_FUNDER="0x..."  (optional, for proxy wallets)
"""

import os
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
    """Place a fill-or-kill market order.

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
