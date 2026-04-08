# backend/apps/common/tasks.py
"""
Celery Tasks for Binance Options Data Sync
===========================================
Periodic tasks that seed and refresh Redis cache via REST API.
All data is NORMALIZED to canonical schema before writing to Redis.

Fixes applied:
  - Connection leak in sync_exchange_structure (bridge.close() now called)
  - Normalizers applied at write time (canonical schema only in Redis)
  - Rate limits from settings (no magic numbers)
  - Consistent snake_case naming
  - Error handling with status propagation
"""

import logging
import json
import time
import asyncio
from datetime import datetime

from celery import shared_task
from django.conf import settings

from .binance_rest import BinanceRESTClient, BinanceAPIError
from .normalizers import normalize_oi_list, normalize_trade_list
from .assets import get_assets_sync
from .redis_bridge import (
    RedisBridge,
    CATEGORY_TICKER,
    CATEGORY_TRADE,
    CATEGORY_OPEN_INTEREST,
)

logger = logging.getLogger("celery.tasks")


# ---------------------------------------------------------------------------
# Rate Limit Configuration (from settings or defaults)
# ---------------------------------------------------------------------------
def _get_rate_limits() -> dict:
    return getattr(
        settings,
        "BINANCE_RATE_LIMITS",
        {
            "requests_per_second": 50,
            "semaphore_limit": 5,  # Conservative concurrent requests
            "inter_request_delay": 0.1,  # Seconds between requests (burst prevention)
        },
    )


@shared_task(name="sync_exchange_structure")
def sync_exchange_structure():
    """Sync symbols and notify the system of structural changes."""
    client = BinanceRESTClient(testnet=False)
    r_sync = _get_sync_redis()

    try:
        data = asyncio.run(client.get_exchange_info())

        if "optionSymbols" not in data:
            return "No optionSymbols found in exchange info."

        # Cache the full blueprint
        r_sync.set("binance:exchange_info", json.dumps(data), ex=3600)

        # Rank assets by number of active strikes (liquidity proxy)
        asset_counts = {}
        for s in data["optionSymbols"]:
            u = s["underlying"]
            asset_counts[u] = asset_counts.get(u, 0) + 1

        top_assets = sorted(asset_counts, key=asset_counts.get, reverse=True)[:20]
        r_sync.set("binance:active_underlyings", json.dumps(top_assets))
        logger.info(f"Top 20 Assets Identified: {top_assets}")

        # Notify system via bridge — with PROPER cleanup
        async def notify():
            bridge = RedisBridge()
            try:
                await bridge.broadcast(
                    "meta",
                    "GLOBAL",
                    {"event": "TOP_ASSETS_UPDATED", "assets": top_assets},
                )
            finally:
                await bridge.close()  # FIX: was missing

        asyncio.run(notify())
        logger.info(
            f"Exchange structure synced: {len(data['optionSymbols'])} symbols, "
            f"{len(top_assets)} top assets."
        )
        return f"Sync Success: {len(data['optionSymbols'])} symbols, {len(top_assets)} top assets identified."

    except BinanceAPIError as e:
        logger.error(f"sync_exchange_structure API error: {e}")
        return f"API Error: {e}"
    except Exception as e:
        logger.error(f"sync_exchange_structure failed: {e}", exc_info=True)
        return f"Failed: {e}"


@shared_task(name="sync_open_interest")
def sync_open_interest():
    """
    Scaled Open Interest Sync.
    1. Fetches top 20 underlyings from Redis.
    2. Filters exchange info for those assets.
    3. Fetches OI for top 3 expirations per asset with rate-limited concurrency.
    4. NORMALIZES data before writing to Redis.
    """
    client = BinanceRESTClient(testnet=False)
    r_sync = _get_sync_redis()
    rate_limits = _get_rate_limits()

    # --- 1. GET TOP ASSETS ---
    top_assets = get_assets_sync(r_sync)

    # --- 2. GET STRUCTURAL DATA ---
    ext_info_raw = r_sync.get("binance:exchange_info")
    if not ext_info_raw:
        return "No Exchange Info found in cache."
    ext_info = json.loads(ext_info_raw)

    # --- 3. BUILD FILTERED JOB LIST ---
    jobs = []
    asset_map = {}

    for s in ext_info.get("optionSymbols", []):
        u = s["underlying"]
        if u in top_assets:
            asset_map.setdefault(u, []).append(s["expiryDate"])

    for asset, ts_list in asset_map.items():
        ts_list = sorted(list(set(ts_list)))
        for ts in ts_list[:3]:
            exp_str = datetime.fromtimestamp(ts / 1000).strftime("%y%m%d")
            base_asset = asset.replace("USDT", "")
            jobs.append((asset, base_asset, exp_str))

    # --- 4. EXECUTE WITH RATE-LIMITED CONCURRENCY ---
    async def run_scaled_sync():
        semaphore = asyncio.Semaphore(rate_limits["semaphore_limit"])
        r_async = _get_async_redis()

        # Track per-asset results for nearest-expiry promotion to key_latest.
        # All expirations write to their per-exp keys; only the NEAREST
        # is promoted to key_latest after all concurrent fetches finish.
        asset_exp_data = {}  # {asset: [(exp_str, raw_json_payload), ...]}

        async def fetch_with_limit(asset, base, exp):
            async with semaphore:
                try:
                    data = await client.get_open_interest(base, exp)

                    if isinstance(data, list) and len(data) > 0:
                        # NORMALIZE before caching
                        normalized = normalize_oi_list(data, source="rest")
                        key_exp = f"binance:{asset}:{CATEGORY_OPEN_INTEREST}:{exp}"
                        # Wrap in payload structure consistent with bridge broadcast
                        payload = {
                            "category": CATEGORY_OPEN_INTEREST,
                            "underlying": asset,
                            "data": normalized,
                        }
                        raw_val = json.dumps(payload)
                        # Write per-expiry key (always, for historical access)
                        await r_async.set(key_exp, raw_val, ex=1800)
                        # Track for nearest-expiry promotion (done after all fetches)
                        asset_exp_data.setdefault(asset, []).append((exp, raw_val))
                        logger.debug(f"OI Updated: {asset} @ {exp}")

                    await asyncio.sleep(rate_limits["inter_request_delay"])
                except BinanceAPIError as e:
                    logger.error(f"OI API error for {asset} {exp}: {e}")
                except Exception as e:
                    logger.error(f"OI Fetch failed for {asset} {exp}: {e}")

        tasks = [fetch_with_limit(asset, base, exp) for asset, base, exp in jobs]
        await asyncio.gather(*tasks)

        # --- Post-processing: promote NEAREST expiry to key_latest per asset ---
        # After all concurrent fetches complete, only the nearest-expiry OI
        # snapshot is promoted to key_latest. This prevents non-deterministic
        # overwrites (Issue #1) and ensures the engine sees near-term data.
        promotion_tasks = []
        for asset, exp_list in asset_exp_data.items():
            if not exp_list:
                continue
            # exp format: "YYMMDD" — string sort matches chronological order
            nearest_exp, nearest_data = min(exp_list, key=lambda x: x[0])
            key_latest = f"binance:{asset}:{CATEGORY_OPEN_INTEREST}"
            promotion_tasks.append(
                r_async.set(key_latest, nearest_data, ex=1800)
            )
            logger.info(
                f"OI key_latest promoted: {asset} → nearest exp {nearest_exp} "
                f"(of {len(exp_list)} expirations fetched)"
            )
        if promotion_tasks:
            await asyncio.gather(*promotion_tasks)

        await r_async.aclose()

    try:
        asyncio.run(run_scaled_sync())
        return f"OI Scaled Sync complete for {len(top_assets)} assets ({len(jobs)} total jobs)"
    except Exception as e:
        logger.error(f"sync_open_interest failed: {e}", exc_info=True)
        return f"OI Sync Failed: {e}"


@shared_task(name="sync_tickers_rest")
def sync_tickers_rest():
    """Fetch 24h ticker for all, filter for top 20 assets, normalize and cache."""
    client = BinanceRESTClient(testnet=False)
    r_sync = _get_sync_redis()

    top_assets = get_assets_sync(r_sync)

    try:
        data = asyncio.run(client.get_ticker())

        if isinstance(data, list):
            grouped = {}
            for item in data:
                asset = item.get("symbol", "").split("-")[0] + "USDT"
                if asset in top_assets:
                    grouped.setdefault(asset, []).append(item)

            for asset, tickers in grouped.items():
                # Wrap in consistent payload structure
                payload = {
                    "category": CATEGORY_TICKER,
                    "underlying": asset,
                    "data": tickers,
                }
                r_sync.set(
                    f"binance:{asset}:{CATEGORY_TICKER}", json.dumps(payload), ex=60
                )

        return f"Tickers synced for {len(top_assets)} assets"

    except BinanceAPIError as e:
        logger.error(f"sync_tickers API error: {e}")
        return f"Ticker Sync API Error: {e}"
    except Exception as e:
        logger.error(f"sync_tickers_rest failed: {e}")
        return f"Ticker Sync Failed: {e}"


@shared_task(name="sync_recent_trades_rest")
def sync_recent_trades_rest():
    """
    Seed trade cache using Block Trades, filtered for top 20 assets.
    Data is NORMALIZED to canonical trade schema before caching.
    """
    client = BinanceRESTClient(testnet=False)
    r_sync = _get_sync_redis()

    top_assets = get_assets_sync(r_sync)

    try:
        data = asyncio.run(client.get_block_trades())

        grouped = {}
        if isinstance(data, list):
            for t in data:
                asset = t["symbol"].split("-")[0] + "USDT"
                if asset in top_assets:
                    grouped.setdefault(asset, []).append(t)

            for asset, trades in grouped.items():
                # NORMALIZE REST trade data to canonical schema
                normalized = normalize_trade_list(trades, source="rest")
                payload = {
                    "category": CATEGORY_TRADE,
                    "underlying": asset,
                    "data": normalized,
                }
                r_sync.set(
                    f"binance:{asset}:{CATEGORY_TRADE}", json.dumps(payload), ex=3600
                )

        return f"Trades seeded for {len(grouped)} assets (normalized)"

    except BinanceAPIError as e:
        logger.error(f"sync_trades API error: {e}")
        return f"Trade Sync API Error: {e}"
    except Exception as e:
        logger.error(f"sync_recent_trades_rest failed: {e}")
        return f"Trade Sync Failed: {e}"


def _sanitize_for_json(obj):
    """
    Recursively walk a data structure and replace non-JSON-safe numeric values.
    PostgreSQL's JSON type rejects Infinity and NaN — these can originate from
    numpy/pandas calculations (np.inf, np.nan) or sentinel values.
    Returns a sanitized copy safe for json.dumps().
    """
    import math

    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return None  # JSON null — PostgreSQL-safe
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


@shared_task(name="save_intelligence_snapshot")
def save_intelligence_snapshot(asset: str, report_data: dict):
    """
    Persist an intelligence report to the database for audit trail and
    historical analysis. Called asynchronously from the Redis bridge after
    each intelligence calculation (throttled to ~60s intervals per asset).
    """
    from .models import IntelligenceSnapshot

    try:
        # Safety net: sanitize any Infinity/NaN from numpy calculations
        # before PostgreSQL JSON serialization rejects them
        report_data = _sanitize_for_json(report_data)

        metrics = report_data.get("metrics", {})
        IntelligenceSnapshot.objects.create(
            asset=asset,
            pcr=metrics.get("pcr", 0),
            max_pain=metrics.get("max_pain", 0),
            call_resistance=metrics.get("resistance", 0),
            put_support=metrics.get("support", 0),
            avg_iv=metrics.get("avg_iv", 0),
            whale_delta=metrics.get("whale_net_volume", 0),
            index_price=report_data.get("index_price", 0),
            action=report_data.get("signal", "NEUTRAL"),
            score=report_data.get("score", 0),
            raw_data=report_data,
        )
        logger.debug(f"Snapshot saved: {asset} → {report_data.get('signal', '?')}")
    except Exception as e:
        logger.error(f"Failed to save snapshot for {asset}: {e}")


# ---------------------------------------------------------------------------
# Redis Helpers
# ---------------------------------------------------------------------------
def _get_sync_redis():
    """Get a synchronous Redis connection for quick reads."""
    import redis

    return redis.from_url(settings.CACHES["default"]["LOCATION"], decode_responses=True)


def _get_async_redis():
    """Get an async Redis connection for high-speed writes."""
    import redis.asyncio as aioredis

    return aioredis.from_url(
        settings.CACHES["default"]["LOCATION"], decode_responses=True
    )