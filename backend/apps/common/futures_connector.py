"""
Futures WebSocket Connector
============================
Connects to Binance USDM Futures kline WebSocket streams.
Routes completed candles to the FuturesOrchestrator callback.

Reads all WS URLs from broker_config.py — no hardcoded values.
Uses exponential backoff reconnection.
"""

import asyncio
import json
import logging
import websockets

from .broker_config import get_broker_config, DEFAULT_BROKER

logger = logging.getLogger("futures.connector")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RECONNECT_BASE_DELAY = 5.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_MULTIPLIER = 2.0

WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 10
WS_CLOSE_TIMEOUT = 5
WS_OPEN_TIMEOUT = 10


class FuturesConnector:
    """
    Broker-agnostic Futures kline WebSocket connector.
    Subscribes to 1m kline streams and fires on_candle_complete
    callback when a candle closes (x: true).
    """

    def __init__(self, broker: str = DEFAULT_BROKER):
        self.broker = broker
        self.on_candle_complete = None  # Callback: async def(symbol, k_data)
        self._running = False
        self._task = None

        # Read WS URLs from broker_config
        config = get_broker_config(broker)
        futures = config["futures"]
        self.ws_base_url = futures["ws_base_url"]

    async def start(self, symbols: list):
        """
        Subscribe to 1m kline streams for all symbols.
        Runs until stop() is called.
        """
        self._running = True

        # Build combined stream URI
        streams = []
        for sym in symbols:
            streams.append(f"{sym.lower()}@kline_1m")

        uri = f"{self.ws_base_url}/stream?streams={'/'.join(streams)}"
        logger.info(f"Starting Futures WS for {len(symbols)} symbols: {symbols}")
        await self._consume(uri)

    async def stop(self):
        """Signal the connector to stop (used by signal handler)."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _consume(self, uri: str):
        """Consume the kline WebSocket stream with reconnection."""
        delay = RECONNECT_BASE_DELAY

        while self._running:
            try:
                logger.info(f"Connecting to Futures kline stream (retry in {delay:.1f}s)...")
                async with websockets.connect(
                    uri,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=WS_CLOSE_TIMEOUT,
                    open_timeout=WS_OPEN_TIMEOUT,
                ) as ws:
                    logger.info("SUCCESS: Connected to Futures kline stream.")
                    delay = RECONNECT_BASE_DELAY
                    msg_count = 0

                    while self._running:
                        msg = await ws.recv()
                        packet = json.loads(msg)
                        msg_count += 1

                        if msg_count % 100 == 0:
                            logger.debug(f"Futures stream: {msg_count} messages received")

                        stream_name = packet.get("stream", "")
                        data = packet.get("data", {})

                        # data is a kline event: { "e": "kline", "E": ..., "k": { ... } }
                        k = data.get("k", {})

                        if k.get("x"):  # Candle is complete
                            symbol = k.get("s", "")
                            if self.on_candle_complete:
                                try:
                                    await self.on_candle_complete(symbol, k)
                                except Exception as e:
                                    logger.error(f"on_candle_complete error for {symbol}: {e}")

            except asyncio.CancelledError:
                logger.info("Futures stream task cancelled. Shutting down.")
                raise
            except websockets.ConnectionClosed as e:
                logger.warning(f"Futures WS closed (code={e.code}): {e.reason}")
            except websockets.InvalidStatusCode as e:
                logger.error(f"Futures WS invalid status: {e.status_code}")
            except Exception as e:
                logger.error(f"Futures WS error: {e}", exc_info=True)

            if self._running:
                logger.warning(f"Reconnecting Futures stream in {delay:.1f}s...")
                await asyncio.sleep(delay)
                delay = min(delay * RECONNECT_MULTIPLIER, RECONNECT_MAX_DELAY)
