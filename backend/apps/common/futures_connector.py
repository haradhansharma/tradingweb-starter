# backend/apps/common/futures_connector.py
"""
Futures WebSocket Connector
=============================
Connects to Binance USDM Futures WebSocket for real-time kline data.
Completely separate from BinanceOptionsConnector (Options WS).

Data flow:
  fstream.binance.com/market → kline ticks → on_candle_complete callback
  The callback (provided by FuturesOrchestrator) handles:
    - KlineStore.update_candle()
    - IndicatorEngine.calculate_all()
    - Redis cache + pub/sub publishing

Architecture note:
  - Uses /market endpoint (not /public) for regular market data
  - Combined stream mode: ?streams=symbol1@kline_1m/symbol2@kline_1m
  - 250ms update speed per candle
  - Reconnection with exponential backoff
  - Ping/pong keepalive via websockets library
"""

import asyncio
import json
import logging
import websockets

logger = logging.getLogger("futures.connector")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WS_BASE_URL = "wss://fstream.binance.com/market"

# Reconnection: 5s → 10s → 20s → 40s → 60s (capped)
RECONNECT_BASE_DELAY = 5.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_MULTIPLIER = 2.0

# WebSocket keepalive
WS_PING_INTERVAL = 20   # seconds between pings
WS_PING_TIMEOUT = 10    # seconds to wait for pong
WS_CLOSE_TIMEOUT = 5    # seconds for close handshake
WS_OPEN_TIMEOUT = 10    # seconds to wait for WS handshake

# Default interval for kline subscription
DEFAULT_KLINE_INTERVAL = "1m"


class FuturesConnector:
    """
    Binance USDM Futures WebSocket connector for kline streams.

    Usage:
        connector = FuturesConnector()
        connector.on_candle_complete = my_callback
        await connector.start(["BTCUSDT", "ETHUSDT"])
    """

    def __init__(self):
        self.on_candle_complete = None  # callback(symbol: str, completed: bool)
        self.on_candle_update = None    # callback(symbol: str) — every tick
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self, symbols: list, interval: str = DEFAULT_KLINE_INTERVAL):
        """
        Start consuming kline WebSocket streams.

        Parameters
        ----------
        symbols : list    e.g. ["BTCUSDT", "ETHUSDT"]
        interval : str    e.g. "1m"
        """
        self._running = True

        streams = []
        for sym in symbols:
            streams.append(f"{sym.lower()}@kline_{interval}")

        uri = f"{WS_BASE_URL}/stream?streams={'/'.join(streams)}"
        logger.info(f"Futures WS connecting: {len(symbols)} symbols @ {interval}")

        delay = RECONNECT_BASE_DELAY

        while self._running:
            try:
                logger.info(
                    f"Futures WS: connecting (retry in {delay:.1f}s if fails)..."
                )
                async with websockets.connect(
                    uri,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=WS_CLOSE_TIMEOUT,
                    open_timeout=WS_OPEN_TIMEOUT,
                ) as ws:
                    logger.info("Futures WS: CONNECTED")
                    delay = RECONNECT_BASE_DELAY
                    msg_count = 0

                    while self._running:
                        msg = await ws.recv()
                        packet = json.loads(msg)
                        msg_count += 1

                        if msg_count % 500 == 0:
                            logger.debug(
                                f"Futures WS: {msg_count} messages received"
                            )

                        stream_name = packet.get("stream", "")
                        data = packet.get("data", {})

                        if not data:
                            continue

                        # Route kline event
                        event_type = data.get("e", "")
                        if event_type == "kline":
                            await self._handle_kline(data, stream_name)

            except asyncio.CancelledError:
                logger.info("Futures WS: task cancelled, shutting down.")
                raise
            except websockets.ConnectionClosed as e:
                logger.warning(
                    f"Futures WS: closed (code={e.code}): {e.reason}"
                )
            except websockets.InvalidStatusCode as e:
                logger.error(f"Futures WS: invalid status {e.status_code}")
            except Exception as e:
                logger.error(f"Futures WS: error: {e}", exc_info=True)

            if not self._running:
                break

            # Exponential backoff
            logger.warning(f"Futures WS: reconnecting in {delay:.1f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * RECONNECT_MULTIPLIER, RECONNECT_MAX_DELAY)

    async def stop(self):
        """Signal the connector to stop. Call before closing event loop."""
        self._running = False
        logger.info("Futures WS: stop requested")

    async def _handle_kline(self, data: dict, stream_name: str):
        """
        Process a kline event from the WebSocket.

        Extracts symbol and k data, calls registered callbacks.
        """
        k = data.get("k", {})
        if not k:
            return

        # Symbol from the kline event
        symbol = k.get("s", "").upper()
        if not symbol:
            # Fallback: extract from stream name
            symbol = stream_name.split("@")[0].upper()

        is_complete = k.get("x", False)

        if is_complete:
            if self.on_candle_complete:
                try:
                    await self.on_candle_complete(symbol, k)
                except Exception as e:
                    logger.error(
                        f"Futures WS: candle_complete callback error "
                        f"for {symbol}: {e}", exc_info=True
                    )

        if self.on_candle_update:
            try:
                await self.on_candle_update(symbol, k)
            except Exception as e:
                logger.error(
                    f"Futures WS: candle_update callback error "
                    f"for {symbol}: {e}", exc_info=True
                )
