"""WebSocket client for Polymarket CLOB real-time events.

Provides two channels:
  - User channel: fill notifications, order updates (authenticated)
  - Market channel: price changes, book updates, last trades (public)

Uses the websockets library (optional dependency, like py-clob-client).

WebSocket endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/

Market channel events:
  - book: full order book snapshot
  - price_change: order placed/canceled affecting a price level
  - last_trade_price: trade executed (price, size, side)
  - best_bid_ask: best bid/ask changed

User channel events (authenticated):
  - order PLACEMENT: new order accepted
  - order UPDATE: partial fill (size_matched updated)
  - order CANCELLATION: order canceled
  - trade: order matched (includes maker_orders with matched_amount)
"""

import json
import threading
import time
from dataclasses import dataclass, field

WSS_BASE = "wss://ws-subscriptions-clob.polymarket.com/ws/"


@dataclass
class PriceUpdate:
    """A price update from the market channel."""

    asset_id: str
    price: float
    timestamp: float


@dataclass
class FillUpdate:
    """A fill update from the user channel."""

    order_id: str
    size_matched: float
    original_size: float
    status: str  # "PLACEMENT", "UPDATE", "CANCELLATION"
    price: float


class MarketStream:
    """Stream real-time price updates for a token via WebSocket.

    Used for stop-loss monitoring — sub-second price updates instead
    of polling REST every 5 seconds.

    Usage:
        stream = MarketStream(token_id)
        stream.start()
        ...
        price = stream.last_price  # latest price, updated in real-time
        ...
        stream.stop()
    """

    def __init__(self, token_id: str, condition_id: str | None = None):
        self.token_id = token_id
        self.condition_id = condition_id
        self.last_price: float | None = None
        self.last_bid: float | None = None
        self.last_ask: float | None = None
        self.last_update: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._callbacks: list = []

    def on_price(self, callback) -> None:
        """Register a callback for price updates: callback(PriceUpdate)."""
        self._callbacks.append(callback)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        try:
            import websockets.sync.client as ws_client
        except ImportError:
            # Fallback: websockets not installed, degrade to no-op
            return

        while self._running:
            try:
                with ws_client.connect(WSS_BASE + "market", open_timeout=10) as ws:
                    # Subscribe to the market's assets
                    sub_msg = {
                        "type": "market",
                        "assets_ids": [self.token_id],
                    }
                    if self.condition_id:
                        sub_msg["markets"] = [self.condition_id]
                    ws.send(json.dumps(sub_msg))

                    while self._running:
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue

                        self._handle_message(raw)
            except Exception:
                if self._running:
                    time.sleep(2)  # reconnect backoff

    def _handle_message(self, raw: str) -> None:
        try:
            msgs = json.loads(raw)
            if not isinstance(msgs, list):
                msgs = [msgs]

            for msg in msgs:
                event_type = msg.get("event_type", "")

                if event_type == "last_trade_price":
                    price = float(msg.get("price", 0))
                    if price > 0 and msg.get("asset_id") == self.token_id:
                        self.last_price = price
                        self.last_update = time.time()
                        self._notify(price)

                elif event_type == "best_bid_ask":
                    if msg.get("asset_id") == self.token_id:
                        bid = msg.get("best_bid")
                        ask = msg.get("best_ask")
                        if bid:
                            self.last_bid = float(bid)
                        if ask:
                            self.last_ask = float(ask)
                        # Use midpoint as price estimate
                        if self.last_bid and self.last_ask:
                            mid = (self.last_bid + self.last_ask) / 2
                            self.last_price = mid
                            self.last_update = time.time()
                            self._notify(mid)

                elif event_type == "book":
                    # Full book snapshot — extract best bid
                    bids = msg.get("bids", [])
                    if bids and msg.get("asset_id") == self.token_id:
                        best_bid = max(float(b["price"]) for b in bids)
                        self.last_bid = best_bid
                        self.last_price = best_bid
                        self.last_update = time.time()
                        self._notify(best_bid)

                elif event_type == "price_change":
                    changes = msg.get("changes", [])
                    for change in changes:
                        if change.get("asset_id") == self.token_id:
                            best_bid = change.get("best_bid")
                            best_ask = change.get("best_ask")
                            if best_bid:
                                self.last_bid = float(best_bid)
                            if best_ask:
                                self.last_ask = float(best_ask)
                            if self.last_bid and self.last_ask:
                                mid = (self.last_bid + self.last_ask) / 2
                                self.last_price = mid
                                self.last_update = time.time()
                                self._notify(mid)

        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    def _notify(self, price: float) -> None:
        update = PriceUpdate(
            asset_id=self.token_id,
            price=price,
            timestamp=time.time(),
        )
        for cb in self._callbacks:
            try:
                cb(update)
            except Exception:
                pass


class UserStream:
    """Stream order fill events for the authenticated user.

    Used for real-time fill notifications instead of polling
    GET /data/order/<id>.

    Usage:
        stream = UserStream(api_key)
        stream.start()
        ...
        fills = stream.get_fills(order_id)  # fills for a specific order
        ...
        stream.stop()
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key
        self._running = False
        self._thread: threading.Thread | None = None
        self._fills: dict[str, FillUpdate] = {}
        self._lock = threading.Lock()
        self._callbacks: list = []

    def on_fill(self, callback) -> None:
        """Register a callback for fill updates: callback(FillUpdate)."""
        self._callbacks.append(callback)

    def get_fill(self, order_id: str) -> FillUpdate | None:
        """Get the latest fill update for an order."""
        with self._lock:
            return self._fills.get(order_id)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        try:
            import websockets.sync.client as ws_client
        except ImportError:
            return

        while self._running:
            try:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"

                with ws_client.connect(
                    WSS_BASE + "user",
                    open_timeout=10,
                    additional_headers=headers,
                ) as ws:
                    sub_msg = {"type": "user"}
                    if self.api_key:
                        sub_msg["auth"] = self.api_key
                    ws.send(json.dumps(sub_msg))

                    while self._running:
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue

                        self._handle_message(raw)
            except Exception:
                if self._running:
                    time.sleep(2)

    def _handle_message(self, raw: str) -> None:
        try:
            msgs = json.loads(raw)
            if not isinstance(msgs, list):
                msgs = [msgs]

            for msg in msgs:
                # Order update events (PLACEMENT, UPDATE, CANCELLATION)
                order_id = msg.get("order_id") or msg.get("id")
                if not order_id:
                    continue

                fill = FillUpdate(
                    order_id=order_id,
                    size_matched=float(msg.get("size_matched", 0)),
                    original_size=float(msg.get("original_size", 0)),
                    status=str(msg.get("type", msg.get("status", "unknown"))),
                    price=float(msg.get("price", 0)),
                )

                with self._lock:
                    self._fills[order_id] = fill

                for cb in self._callbacks:
                    try:
                        cb(fill)
                    except Exception:
                        pass

        except (json.JSONDecodeError, ValueError, KeyError):
            pass
