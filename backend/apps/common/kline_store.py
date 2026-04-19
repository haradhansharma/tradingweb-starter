"""
Kline Store — Redis OHLCV Storage + Multi-Timeframe Aggregation
================================================================
Stores completed 1m candles in Redis, aggregates to higher timeframes.
Designed as a standalone module — no coupling to intelligence engine.

Redis Keys (DB0 — Cache):
  kline:{SYMBOL}:1m:history    → Sorted Set (score=open_time, value=candle JSON)
  kline:{SYMBOL}:1m:current    → String (JSON — current in-progress candle)
  kline:{SYMBOL}:{TF}:history  → Sorted Set (aggregated from 1m)
  indicators:{SYMBOL}          → Hash (field=value pairs for all TFs + indicators)

"""

import json
import time
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger("kline.store")

# ---------------------------------------------------------------------------
# Timeframe mapping: how many 1m candles fit in each higher TF
# ---------------------------------------------------------------------------
TF_MULTIPLIER = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}

# TFs rebuilt from scratch on every 1m candle completion (fast — plenty of 1m data)
FULL_REBUILD_TFS = ["5m", "15m"]

# TFs seeded directly via REST and incrementally appended via WS.
# NOT rebuilt from 1m each tick (would lose directly-seeded history).
APPEND_TFS = ["1h", "4h"]

# Backward alias — used by orchestrator to iterate all higher TFs
DEFAULT_AGGREGATE_TFS = FULL_REBUILD_TFS + APPEND_TFS

# How many 1m candles to keep in Redis (1500 = 25 hours, matches REST max)
MAX_1M_CANDLES = 1500

# How many candles to keep for aggregated TFs
MAX_TF_CANDLES = 500

# TTL for indicator cache (seconds)
INDICATOR_TTL = 300

# Fields expected in a normalized candle dict
CANDLE_FIELDS = ("t", "o", "h", "l", "c", "v", "T", "q", "n", "V", "Q")


def normalize_kline(raw: list) -> Optional[Dict]:
    """
    Convert raw Binance kline array to a normalized dict.

    Raw format (from REST):
    [open_time, open, high, low, close, volume, close_time,
     quote_volume, trades, taker_buy_volume, taker_buy_quote_volume, ignore]

    Raw format (from WS k.k):
    {t, o, h, l, c, v, T, q, n, V, Q, x, ...}
    """
    if isinstance(raw, list):
        if len(raw) < 11:
            return None
        return {
            "t": int(raw[0]),       # open time (ms)
            "o": float(raw[1]),     # open
            "h": float(raw[2]),     # high
            "l": float(raw[3]),     # low
            "c": float(raw[4]),     # close
            "v": float(raw[5]),     # base asset volume
            "T": int(raw[6]),       # close time (ms)
            "q": float(raw[7]),     # quote asset volume
            "n": int(raw[8]),       # number of trades
            "V": float(raw[9]),     # taker buy base volume
            "Q": float(raw[10]),    # taker buy quote volume
        }
    elif isinstance(raw, dict):
        k = raw.get("k", raw)
        try:
            return {
                "t": int(k["t"]),
                "o": float(k["o"]),
                "h": float(k["h"]),
                "l": float(k["l"]),
                "c": float(k["c"]),
                "v": float(k["v"]),
                "T": int(k["T"]),
                "q": float(k["q"]),
                "n": int(k.get("n", 0)),
                "V": float(k.get("V", 0)),
                "Q": float(k.get("Q", 0)),
            }
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Failed to normalize kline dict: {e}")
            return None
    return None


def aggregate_candles(candles_1m: List[Dict], multiplier: int) -> List[Dict]:
    """
    Aggregate 1m candles into a higher timeframe.

    Algorithm:
    1. Group 1m candles by their higher-TF bucket
    2. For each group: open=first.open, high=max(highs), low=min(lows),
       close=last.close, volume=sum(volumes), etc.
    3. A group is COMPLETE only when all its 1m members are present
       (we rely on the caller to only pass completed 1m candles).
    """
    if not candles_1m or multiplier < 1:
        return []

    # Sort by open time
    sorted_candles = sorted(candles_1m, key=lambda x: x["t"])

    # Group into higher-TF buckets
    groups = {}
    for candle in sorted_candles:
        open_time = candle["t"]
        # Find the bucket start time
        # Bucket = floor(open_time / (multiplier * 60000)) * (multiplier * 60000)
        bucket_ms = multiplier * 60 * 1000
        bucket_start = (open_time // bucket_ms) * bucket_ms

        if bucket_start not in groups:
            groups[bucket_start] = []
        groups[bucket_start].append(candle)

    result = []
    for bucket_start in sorted(groups.keys()):
        group = groups[bucket_start]
        if len(group) < multiplier:
            # Incomplete bucket — skip (not enough 1m candles yet)
            continue

        result.append({
            "t": bucket_start,
            "o": group[0]["o"],
            "h": max(c["h"] for c in group),
            "l": min(c["l"] for c in group),
            "c": group[-1]["c"],
            "v": sum(c["v"] for c in group),
            "T": group[-1]["T"],
            "q": sum(c["q"] for c in group),
            "n": sum(c["n"] for c in group),
            "V": sum(c["V"] for c in group),
            "Q": sum(c["Q"] for c in group),
        })

    return result


class KlineStore:
    """
    Redis-backed OHLCV storage with multi-timeframe aggregation.
    Uses async Redis for non-blocking operations.
    """

    def __init__(self, cache_client, broker: str = "binance"):
        """
        Parameters
        ----------
        cache_client : redis.asyncio.Redis    — DB0 connection for cache  
        broker : str                           — Broker name for Redis key prefix
        """
        self.cache = cache_client    
        self.broker = broker

    # ------------------------------------------------------------------
    # Seed Historical Data (from REST)
    # ------------------------------------------------------------------

    async def seed_history(self, symbol: str, raw_klines: list, interval: str = "1m") -> int:
        """
        Seed candle history from REST response. Called once on startup.
        Supports any interval — 1m, 5m, 15m, 1h, 4h, etc.

        Parameters
        ----------
        symbol : str          e.g. "BTCUSDT"
        raw_klines : list     Raw Binance kline response (list of lists)
        interval : str        e.g. "1m", "1h", "4h"

        Returns
        -------
        int — number of candles saved
        """
        candles = []
        for raw in raw_klines:
            norm = normalize_kline(raw)
            if norm:
                candles.append(norm)

        if not candles:
            logger.warning(f"No valid klines to seed for {symbol} @ {interval}")
            return 0

        # Use pipeline for batch write
        pipe = self.cache.pipeline()
        key = f"kline:{self.broker}:{symbol}:{interval}:history"

        for candle in candles:
            # score = open_time, value = candle JSON
            pipe.zadd(key, {json.dumps(candle): candle["t"]})

        max_candles = MAX_1M_CANDLES if interval == "1m" else MAX_TF_CANDLES
        # Trim to keep only latest
        pipe.zremrangebyrank(key, 0, -(max_candles + 1))
        pipe.expire(key, 86400 * 2)  # 2 days TTL

        await pipe.execute()

        # Only rebuild aggregated TFs from 1m data (5m, 15m)
        if interval == "1m":
            await self._aggregate_all(symbol)

        logger.info(f"Seeded {len(candles)} {interval} candles for {symbol}")
        return len(candles)

    # ------------------------------------------------------------------
    # Update from WS Tick
    # ------------------------------------------------------------------

    async def update_candle(self, symbol: str, k_data: dict) -> Optional[bool]:
        """
        Process a WS kline tick. Updates current in-progress candle.
        If candle is complete (x: true), finalizes it.

        Parameters
        ----------
        symbol : str      e.g. "BTCUSDT"
        k_data : dict     The "k" object from WS kline event

        Returns
        -------
        bool or None — True if a candle was completed, None if just updated
        """
        candle = normalize_kline({"k": k_data})
        if not candle:
            return None

        is_complete = k_data.get("x", False)
        key_current = f"kline:{self.broker}:{symbol}:1m:current"
        key_history = f"kline:{self.broker}:{symbol}:1m:history"

        if not is_complete:
            # Update in-progress candle
            await self.cache.set(key_current, json.dumps(candle), ex=120)
            return None

        # --- Candle is COMPLETE ---
        open_time = candle["t"]

        # Remove current candle key
        await self.cache.delete(key_current)

        # Check if this candle already exists in history (dedup)
        existing = await self.cache.zscore(key_history, open_time)
        if existing is not None:
            # Already saved — skip (WS can send duplicate completions)
            return None

        # Add to history
        pipe = self.cache.pipeline()
        pipe.zadd(key_history, {json.dumps(candle): open_time})
        pipe.zremrangebyrank(key_history, 0, -(MAX_1M_CANDLES + 1))
        await pipe.execute()

        # Aggregate to higher TFs (full rebuild for 5m/15m)
        await self._aggregate_all(symbol)

        # Append new candle to append-type TFs (1h, 4h) if bucket boundary hit
        await self._append_boundary_candles(symbol, candle)

        return True  # Candle was completed

    # ------------------------------------------------------------------
    # Read Data
    # ------------------------------------------------------------------

    async def get_candles(
        self, symbol: str, interval: str = "1m", limit: int = 500
    ) -> List[Dict]:
        """
        Get the latest N completed candles for a symbol + interval.

        Returns list of candle dicts sorted by open_time ascending.
        """
        key = f"kline:{self.broker}:{symbol}:{interval}:history"
        if interval == "1m":
            # Also include the current in-progress candle if it exists
            raw_current = await self.cache.get(f"kline:{self.broker}:{symbol}:1m:current")
        else:
            raw_current = None

        # Fetch from sorted set (highest scores = most recent)
        raw_candles = await self.cache.zrevrange(
            key, 0, limit - 1, withscores=False
        )

        candles = []
        for raw in raw_candles:
            try:
                candles.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue

        # Add current in-progress candle at the end
        if raw_current:
            try:
                current = json.loads(raw_current)
                if candles and current["t"] > candles[-1]["t"]:
                    candles.append(current)
                elif not candles:
                    candles.append(current)
            except (json.JSONDecodeError, TypeError):
                pass

        # Sort ascending by open time
        candles.sort(key=lambda x: x["t"])

        return candles[:limit]

    async def get_latest_candle(self, symbol: str, interval: str = "1m") -> Optional[Dict]:
        """Get the single most recent candle (in-progress or completed)."""
        candles = await self.get_candles(symbol, interval, limit=1)
        return candles[-1] if candles else None

    # ------------------------------------------------------------------
    # Multi-TF Aggregation
    # ------------------------------------------------------------------

    async def _aggregate_all(self, symbol: str):
        """Re-aggregate FULL_REBUILD_TFS (5m, 15m) from 1m history.

        NOTE: 1h and 4h are NOT rebuilt here — they are seeded directly
        via REST and incrementally maintained by _append_boundary_candles().
        Rebuilding them from 1m would destroy the directly-seeded history.
        """
        candles_1m = await self.get_candles(symbol, "1m", limit=MAX_1M_CANDLES)

        if not candles_1m:
            return

        pipe = self.cache.pipeline()

        for tf in FULL_REBUILD_TFS:
            multiplier = TF_MULTIPLIER.get(tf)
            if not multiplier:
                continue

            aggregated = aggregate_candles(candles_1m, multiplier)
            if not aggregated:
                continue

            key = f"kline:{self.broker}:{symbol}:{tf}:history"
            # Clear existing and re-populate
            pipe.delete(key)
            for candle in aggregated:
                pipe.zadd(key, {json.dumps(candle): candle["t"]})
            pipe.zremrangebyrank(key, 0, -(MAX_TF_CANDLES + 1))
            pipe.expire(key, 86400 * 2)

        await pipe.execute()

    async def _append_boundary_candles(self, symbol: str, new_candle: dict):
        """Incrementally append aggregated candles to APPEND_TFS (1h, 4h).

        When a new 1m candle completes, check if it's the LAST candle
        of a higher-TF bucket (e.g., 10:59 completes the 1h 10:00 bucket).
        If so, fetch all 1m candles in that bucket, aggregate, and ZADD
        to the higher-TF sorted set (dedup by open_time score).

        This preserves directly-seeded history while keeping data fresh.
        """
        open_time = new_candle["t"]
        one_min_ms = 60 * 1000

        for tf in APPEND_TFS:
            multiplier = TF_MULTIPLIER.get(tf)
            if not multiplier:
                continue

            bucket_ms = multiplier * one_min_ms

            # Is this candle the LAST 1m candle of a higher-TF bucket?
            # e.g., 10:59 is last of 1h bucket → (10:59 + 1min) % 60min == 0
            if (open_time + one_min_ms) % bucket_ms != 0:
                continue

            # Calculate bucket boundaries
            bucket_start = (open_time // bucket_ms) * bucket_ms
            bucket_end = bucket_start + bucket_ms

            # Fetch all 1m candles in this bucket
            key_1m = f"kline:{self.broker}:{symbol}:1m:history"
            raw_candles = await self.cache.zrangebyscore(
                key_1m, bucket_start, bucket_end - 1
            )

            bucket_candles = []
            for r in raw_candles:
                try:
                    bucket_candles.append(json.loads(r))
                except (json.JSONDecodeError, TypeError):
                    continue

            if len(bucket_candles) < multiplier:
                logger.debug(
                    f"Skipping {tf} append for {symbol}: "
                    f"{len(bucket_candles)}/{multiplier} candles in bucket"
                )
                continue

            # Aggregate this bucket
            aggregated = aggregate_candles(bucket_candles, multiplier)
            if not aggregated:
                continue

            agg_candle = aggregated[0]
            key_tf = f"kline:{self.broker}:{symbol}:{tf}:history"

            pipe = self.cache.pipeline()
            # ZADD is idempotent — same score overwrites existing value
            pipe.zadd(key_tf, {json.dumps(agg_candle): agg_candle["t"]})
            pipe.zremrangebyrank(key_tf, 0, -(MAX_TF_CANDLES + 1))
            pipe.expire(key_tf, 86400 * 2)
            await pipe.execute()

            logger.debug(
                f"Appended {tf} candle for {symbol}: "
                f"o={agg_candle['o']} h={agg_candle['h']} "
                f"l={agg_candle['l']} c={agg_candle['c']}"
            )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def delete_symbol(self, symbol: str):
        """Remove all kline data for a symbol (used when asset is removed)."""
        pipe = self.cache.pipeline()
        for tf in list(TF_MULTIPLIER.keys()):
            pipe.delete(f"kline:{self.broker}:{symbol}:{tf}:history")
        pipe.delete(f"kline:{self.broker}:{symbol}:1m:current")
        pipe.delete(f"kline:{self.broker}:{symbol}:indicators")
        await pipe.execute()
        logger.info(f"Cleaned up all kline data for {symbol}")
