"""
Futures Orchestrator
====================
Ties together FuturesConnector + KlineStore + IndicatorEngine.
Runs alongside the options pipeline but is completely independent.

Lifecycle:
  1. seed_from_rest() — fetch 1500x 1m + 200x 1h + 200x 4h candles via REST
  2. start_ws() — begin real-time WS kline stream (1m candles)
  3. On each completed candle → update store → recalculate indicators → publish

Redis usage:
  DB0 (Cache): kline data + indicator values
  DB2 (Channels): Django Channels group → frontend WS
"""

import asyncio
import json
import logging
from typing import Dict, Optional

import redis.asyncio as aioredis
from django.conf import settings
from channels.layers import get_channel_layer

from .futures_rest import FuturesRESTClient
from .futures_connector import FuturesConnector
from .kline_store import KlineStore, DEFAULT_AGGREGATE_TFS
from .indicator_engine import IndicatorEngine
from .market_sessions import compute_sessions
from .broker_config import (
    redis_key,
    ws_group_name,
    ws_universal_group,
    DEFAULT_BROKER,
)

logger = logging.getLogger("futures.orchestrator")

# Category constant for Channels broadcast
CATEGORY_INDICATORS = "indicators"
CATEGORY_FUTURES_PRICE = "futuresPrice"

# How many 1m candles to seed from REST (max per Binance request = 1500)
SEED_1M_COUNT = 1500

# How many higher-TF candles to seed directly (1h, 4h)
# Direct seeding is critical: 1500×1m only aggregates to ~25×1h and ~6×4h
# which starves MACD(35), SMA(50), and other data-hungry indicators.
SEED_HIGHER_TF_COUNT = 200

# Higher TFs to seed directly via REST (not aggregated from 1m)
DIRECT_SEED_TFS = ["1h", "4h"]


class FuturesOrchestrator:
    """
    Orchestrates the futures kline + indicator pipeline.
    Start with run() and stop with stop().
    """

    def __init__(self, broker: str = DEFAULT_BROKER):
        self.connector = FuturesConnector(broker=broker)
        self.store: Optional[KlineStore] = None
        self.engine = IndicatorEngine()
        self.cache: Optional[aioredis.Redis] = None     
        self.channel_layer = None
        self.symbols: list = []
        self.broker = broker
        self._running = False
        self._session_task: Optional[asyncio.Task] = None

    async def _init_connections(self):
        """Initialize Redis and Channel connections."""
        self.cache = aioredis.from_url(
            settings.CACHES["default"]["LOCATION"], decode_responses=True
        )
       
        self.channel_layer = get_channel_layer()
        self.store = KlineStore(self.cache, broker=self.broker)

    async def close(self):
        """Release all connections."""
        try:
            if self.cache:
                await self.cache.aclose()
        except Exception:
            pass
        

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, symbols: list):
        """
        Full lifecycle: seed history → start WS → calculate indicators.

        Parameters
        ----------
        symbols : list    e.g. ["BTCUSDT", "ETHUSDT"]
        """
        await self._init_connections()
        self.symbols = symbols
        self._running = True

        # Register callbacks
        self.connector.on_candle_complete = self._on_candle_complete
        self.connector.on_price_tick = self._on_price_tick
        
        # Step 1: Seed historical data from REST
        await self._seed_history()

        # Step 2: Initial indicator calculation
        for sym in symbols:
            await self._recalculate_indicators(sym)

        # Step 3: Start market session publisher (background)
        self._session_task = asyncio.create_task(self._session_loop())

        # Step 4: Start WS stream (blocking — runs forever)
        try:
            await self.connector.start(symbols)
        finally:
            if self._session_task:
                self._session_task.cancel()
            await self.close()

    async def stop(self):
        """Stop the WS connector and session publisher."""
        self._running = False
        if self._session_task:
            self._session_task.cancel()
        await self.connector.stop()

    # ------------------------------------------------------------------
    # REST Seeding
    # ------------------------------------------------------------------

    async def _seed_history(self):
        """Fetch historical klines via REST for all symbols.

        Hybrid strategy:
          1. Fetch 1500 x 1m → aggregate into 5m (300) + 15m (100)
          2. Fetch 200 x 1h directly → enough for MACD(35), SMA(50)
          3. Fetch 200 x 4h directly → enough for all 4h indicators

        This ensures every registered indicator has sufficient data
        from the very first calculation — no warm-up period needed.
        """
        client = FuturesRESTClient(broker=self.broker)
        try:
            for sym in self.symbols:
                try:
                    # --- 1m: seed 1500 candles (aggregates to 5m, 15m) ---
                    raw_1m = await client.get_klines(
                        symbol=sym, interval="1m", limit=SEED_1M_COUNT
                    )
                    count_1m = await self.store.seed_history(sym, raw_1m, interval="1m")
                    logger.info(f"Seed {sym}: {count_1m} x 1m candles from REST")

                    # --- 1h, 4h: seed directly (much more data than aggregating) ---
                    for tf in DIRECT_SEED_TFS:
                        raw_tf = await client.get_klines(
                            symbol=sym, interval=tf, limit=SEED_HIGHER_TF_COUNT
                        )
                        count_tf = await self.store.seed_history(
                            sym, raw_tf, interval=tf
                        )
                        logger.info(
                            f"Seed {sym}: {count_tf} x {tf} candles from REST (direct)"
                        )

                except Exception as e:
                    logger.error(f"Failed to seed {sym} from REST: {e}")
                    # Continue with other symbols — don't block
                    continue
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # WS Callback
    # ------------------------------------------------------------------

    async def _on_candle_complete(self, symbol: str, k_data: dict):
        """
        Called when a WS kline candle completes (x: true).
        Updates store, recalculates indicators, publishes results.
        """
        if not self.store:
            return

        # Update kline store
        completed = await self.store.update_candle(symbol, k_data)
        if not completed:
            return

        # Recalculate indicators for this symbol
        await self._recalculate_indicators(symbol)

    
    async def _on_price_tick(self, symbol: str, price: float):
        """
        Called on every kline tick (even incomplete candles).
        Publishes the live futures price via Django Channels to frontend.
        Throttled to ~1 update/sec per symbol to avoid flooding.
        """
        now = __import__("time").time()
        # Throttle: max 1 broadcast per second per symbol
        last_key = f"_last_futures_price_tick_{symbol}"
        last_tick = getattr(self, last_key, 0)
        if now - last_tick < 1.0:
            return
        setattr(self, last_key, now)

        if not self.channel_layer:
            return

        payload = {
            "category": CATEGORY_FUTURES_PRICE,
            "underlying": symbol,
            "broker": self.broker,
            "data": {"p": str(price)},
        }
        group_name = ws_group_name(self.broker, symbol, CATEGORY_FUTURES_PRICE)
        try:
            await self.channel_layer.group_send(
                group_name,
                {"type": "market.update", "payload": payload},
            )
        except Exception as e:
            logger.debug(f"Futures price broadcast failed for {symbol}: {e}")

    # ------------------------------------------------------------------
    # Market Session Publisher
    # ------------------------------------------------------------------

    async def _session_loop(self):
        """
        Background task: publish market session state every 30 seconds.
        Sessions are broker-agnostic (Sydney/Tokyo/London/NY are the same
        regardless of broker), so we use a universal Channels group that
        ALL frontends auto-join on connect.
        """
        CATEGORY_SESSIONS = "sessions"
        # Universal group: all connected frontends receive sessions
        # regardless of which broker's orchestrator publishes them.
        GROUP_SESSIONS = ws_universal_group(CATEGORY_SESSIONS)

        while self._running:
            try:
                session_data = compute_sessions()
                payload = {
                    "category": CATEGORY_SESSIONS,
                    "underlying": "_global",
                    "data": session_data,
                }

                # Publish via Channels (DB2) → frontend WS
                if self.channel_layer:
                    try:
                        await self.channel_layer.group_send(
                            GROUP_SESSIONS,
                            {"type": "market.update", "payload": payload},
                        )
                    except Exception as e:
                        logger.debug(f"Session channel broadcast failed: {e}")

                # Cache session state in DB0 for backend strategy/decision reads.
                # Key: "sessions:global" with 120s TTL (auto-refreshed every 30s).
                # Any backend process can do: await cache.get("sessions:global")
                if self.cache:
                    try:
                        session_cache_key = redis_key(self.broker, "_global", "sessions")
                        await self.cache.set(
                            session_cache_key,
                            json.dumps(session_data),
                            ex=120,
                        )
                    except Exception as e:
                        logger.debug(f"Session cache write failed: {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Session publisher error: {e}")

            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # Indicator Calculation + Publishing
    # ------------------------------------------------------------------

    async def _recalculate_indicators(self, symbol: str):
        """
        Fetch candles for all TFs, calculate indicators, cache + publish.
        """
        if not self.store or not self.cache:
            return

        # Gather candles for all relevant timeframes
        all_tfs = ["1m"] + DEFAULT_AGGREGATE_TFS
        candles_by_tf = {}

        for tf in all_tfs:
            candles = await self.store.get_candles(symbol, tf, limit=500)
            if candles:
                candles_by_tf[tf] = candles

        if not candles_by_tf:
            return

        # Calculate indicators + strategy
        try:
            enriched = self.engine.calculate_all_with_strategy(candles_by_tf)
        except Exception as e:
            logger.error(f"Indicator calculation failed for {symbol}: {e}")
            return

        values = enriched.get("values", {})
        strategies = enriched.get("strategies", [])

        if not values:
            return

        # --- Cache indicator VALUES only in Redis Hash (backward compatible) ---
        indicator_key = redis_key(self.broker, symbol, "indicators")
        strategies_key = redis_key(self.broker, symbol, "indicators:strategies")
        pipe = self.cache.pipeline()
        pipe.hset(indicator_key, mapping={k: str(v) for k, v in values.items() if v is not None})
        pipe.expire(indicator_key, 300)  # 5 min TTL
        # Also cache strategies so consumer can serve them on subscribe
        pipe.set(strategies_key, json.dumps(strategies), ex=300)
        await pipe.execute()

        # --- Build WS payload (values + strategies) ---
        # The display config is fetched separately via API to avoid
        # repeating ~3KB of static metadata on every candle tick.
        ws_data = {
            "values": values,
            "strategies": strategies,
        }        

        # --- Broadcast via Django Channels (DB2) → Frontend WS ---
        if self.channel_layer:
            try:
                group_name = ws_group_name(self.broker, symbol, CATEGORY_INDICATORS)
                payload = {
                    "category": CATEGORY_INDICATORS,
                    "underlying": symbol,
                    "broker": self.broker,
                    "data": ws_data,
                }
                await self.channel_layer.group_send(
                    group_name,
                    {"type": "market.update", "payload": payload},
                )
            except Exception as e:
                logger.debug(f"Channel broadcast failed for {symbol}: {e}")

    # ------------------------------------------------------------------
    # REST API Data Access (for frontend chart data)
    # ------------------------------------------------------------------

    async def get_kline_data(
        self, symbol: str, interval: str = "1h", limit: int = 200
    ) -> list:
        """Get kline data for REST API responses (chart rendering)."""
        if not self.store:
            return []
        return await self.store.get_candles(symbol, interval, limit)

    async def get_indicator_data(self, symbol: str) -> dict:
        """Get current indicator values for REST API responses."""
        if not self.cache:
            return {}
        indicator_key = redis_key(self.broker, symbol, "indicators")
        raw = await self.cache.hgetall(indicator_key)
        if not raw:
            return {}
        # Convert string values back to float
        return {k: float(v) for k, v in raw.items() if v is not None}
