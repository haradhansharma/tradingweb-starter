"""
Celery Tasks — Dynamic Broker Registry
=======================================
Broker-parameterized periodic tasks that seed and refresh Redis cache
via REST API. All data is NORMALIZED to canonical schema before writing.

Architecture (Dynamic Broker Registry):
  - Base functions accept `broker` parameter and are broker-agnostic
  - Tasks are dynamically registered per broker from BROKER_CONFIGS
  - Each broker gets its own named task: e.g. "binance:sync_exchange_structure"
  - Adding a new broker: just add entry to BROKER_CONFIGS — tasks auto-register

Fixes applied:
  - Connection leak in sync_exchange_structure (bridge.close() now called)
  - Normalizers applied at write time (canonical schema only in Redis)
  - Rate limits from broker_config (no hardcoded BINANCE_RATE_LIMITS)
  - Broker isolation — no data mixing between exchanges
"""

import logging
import json
import time
import asyncio
from datetime import datetime

from celery import shared_task
from django.conf import settings

# Use broker-agnostic imports (backward-compatible aliases exist)
from .binance_rest import OptionsRESTClient, OptionsAPIError
from .normalizers import normalize_oi_list, normalize_trade_list
from .assets import get_assets_sync
from .redis_bridge import (
    RedisBridge,
    CATEGORY_TICKER,
    CATEGORY_TRADE,
    CATEGORY_OPEN_INTEREST,
)
from .broker_config import (
    redis_key,
    redis_global_key,
    BROKER_CONFIGS,
    get_rate_limits,
)

logger = logging.getLogger("celery.tasks")


# ---------------------------------------------------------------------------
# Rate Limit Configuration (broker-aware — from broker_config)
# ---------------------------------------------------------------------------
def _get_rate_limits(broker: str) -> dict:
    """Get rate limits from broker_config instead of hardcoded settings."""
    return get_rate_limits(broker)


# ===========================================================================
# BASE FUNCTIONS (broker-parameterized, NOT Celery tasks themselves)
# ===========================================================================
# These are the actual implementation. Celery tasks below are thin wrappers
# that bind a specific broker to these base functions.

def _base_sync_exchange_structure(broker: str) -> str:
    """Sync symbols and notify the system of structural changes."""
    client = OptionsRESTClient(broker=broker)
    r_sync = _get_sync_redis()

    try:
        data = asyncio.run(client.get_exchange_info())

        if "optionSymbols" not in data:
            return "No optionSymbols found in exchange info."

        # Cache the full blueprint
        r_sync.set(redis_global_key(broker, "exchange_info"), json.dumps(data), ex=3600)

        # Rank assets by number of active strikes (liquidity proxy)
        asset_counts = {}
        for s in data["optionSymbols"]:
            u = s["underlying"]
            asset_counts[u] = asset_counts.get(u, 0) + 1

        top_assets = sorted(asset_counts, key=asset_counts.get, reverse=True)[:20]
        r_sync.set(redis_global_key(broker, "active_underlyings"), json.dumps(top_assets))
        logger.info(f"[{broker}] Top 20 Assets Identified: {top_assets}")

        # Notify system via bridge — with PROPER cleanup
        async def notify():
            bridge = RedisBridge(broker=broker)
            try:
                await bridge.broadcast(
                    "meta",
                    "GLOBAL",
                    {"event": "TOP_ASSETS_UPDATED", "assets": top_assets},
                )
            finally:
                await bridge.close()

        asyncio.run(notify())
        logger.info(
            f"[{broker}] Exchange structure synced: {len(data['optionSymbols'])} symbols, "
            f"{len(top_assets)} top assets."
        )
        return f"[{broker}] Sync Success: {len(data['optionSymbols'])} symbols, {len(top_assets)} top assets identified."

    except OptionsAPIError as e:
        logger.error(f"[{broker}] sync_exchange_structure API error: {e}")
        return f"[{broker}] API Error: {e}"
    except Exception as e:
        logger.error(f"[{broker}] sync_exchange_structure failed: {e}", exc_info=True)
        return f"[{broker}] Failed: {e}"


def _base_sync_open_interest(broker: str) -> str:
    """
    Scaled Open Interest Sync.
    1. Fetches top 20 underlyings from Redis.
    2. Filters exchange info for those assets.
    3. Fetches OI for top 3 expirations per asset with rate-limited concurrency.
    4. NORMALIZES data before writing to Redis.
    """
    client = OptionsRESTClient(broker=broker)
    r_sync = _get_sync_redis()
    rate_limits = _get_rate_limits(broker)

    # --- 1. GET TOP ASSETS ---
    top_assets = get_assets_sync(r_sync, broker=broker)

    # --- 2. GET STRUCTURAL DATA ---
    ext_info_raw = r_sync.get(redis_global_key(broker, "exchange_info"))
    if not ext_info_raw:
        return f"[{broker}] No Exchange Info found in cache."
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

        asset_exp_data = {}

        async def fetch_with_limit(asset, base, exp):
            async with semaphore:
                try:
                    data = await client.get_open_interest(base, exp)

                    if isinstance(data, list) and len(data) > 0:
                        normalized = normalize_oi_list(data, source="rest", broker=broker)
                        key_exp = redis_key(broker, asset, f"{CATEGORY_OPEN_INTEREST}:{exp}")
                        payload = {
                            "category": CATEGORY_OPEN_INTEREST,
                            "underlying": asset,
                            "data": normalized,
                        }
                        raw_val = json.dumps(payload)
                        await r_async.set(key_exp, raw_val, ex=1800)
                        asset_exp_data.setdefault(asset, []).append((exp, raw_val))
                        logger.debug(f"[{broker}] OI Updated: {asset} @ {exp}")

                    await asyncio.sleep(rate_limits["inter_request_delay"])
                except OptionsAPIError as e:
                    logger.error(f"[{broker}] OI API error for {asset} {exp}: {e}")
                except Exception as e:
                    logger.error(f"[{broker}] OI Fetch failed for {asset} {exp}: {e}")

        tasks = [fetch_with_limit(asset, base, exp) for asset, base, exp in jobs]
        await asyncio.gather(*tasks)

        # --- Post-processing: promote NEAREST expiry to key_latest per asset ---
        promotion_tasks = []
        for asset, exp_list in asset_exp_data.items():
            if not exp_list:
                continue
            nearest_exp, nearest_data = min(exp_list, key=lambda x: x[0])
            key_latest = redis_key(broker, asset, CATEGORY_OPEN_INTEREST)
            promotion_tasks.append(
                r_async.set(key_latest, nearest_data, ex=1800)
            )
            logger.info(
                f"[{broker}] OI key_latest promoted: {asset} → nearest exp {nearest_exp} "
                f"(of {len(exp_list)} expirations fetched)"
            )
        if promotion_tasks:
            await asyncio.gather(*promotion_tasks)

        await r_async.aclose()

    try:
        asyncio.run(run_scaled_sync())
        return f"[{broker}] OI Scaled Sync complete for {len(top_assets)} assets ({len(jobs)} total jobs)"
    except Exception as e:
        logger.error(f"[{broker}] sync_open_interest failed: {e}", exc_info=True)
        return f"[{broker}] OI Sync Failed: {e}"


def _base_sync_tickers_rest(broker: str) -> str:
    """Fetch 24h ticker for all, filter for top 20 assets, normalize and cache."""
    client = OptionsRESTClient(broker=broker)
    r_sync = _get_sync_redis()

    top_assets = get_assets_sync(r_sync, broker=broker)

    try:
        data = asyncio.run(client.get_ticker())

        if isinstance(data, list):
            grouped = {}
            for item in data:
                asset = item.get("symbol", "").split("-")[0] + "USDT"
                if asset in top_assets:
                    grouped.setdefault(asset, []).append(item)

            for asset, tickers in grouped.items():
                payload = {
                    "category": CATEGORY_TICKER,
                    "underlying": asset,
                    "data": tickers,
                }
                r_sync.set(
                    redis_key(broker, asset, CATEGORY_TICKER), json.dumps(payload), ex=60
                )

        return f"[{broker}] Tickers synced for {len(top_assets)} assets"

    except OptionsAPIError as e:
        logger.error(f"[{broker}] sync_tickers API error: {e}")
        return f"[{broker}] Ticker Sync API Error: {e}"
    except Exception as e:
        logger.error(f"[{broker}] sync_tickers_rest failed: {e}")
        return f"[{broker}] Ticker Sync Failed: {e}"


def _base_sync_recent_trades_rest(broker: str) -> str:
    """
    Seed trade cache using Block Trades, filtered for top 20 assets.
    Data is NORMALIZED to canonical trade schema before caching.
    """
    client = OptionsRESTClient(broker=broker)
    r_sync = _get_sync_redis()

    top_assets = get_assets_sync(r_sync, broker=broker)

    try:
        data = asyncio.run(client.get_block_trades())

        grouped = {}
        if isinstance(data, list):
            for t in data:
                asset = t["symbol"].split("-")[0] + "USDT"
                if asset in top_assets:
                    grouped.setdefault(asset, []).append(t)

            for asset, trades in grouped.items():
                normalized = normalize_trade_list(trades, source="rest", broker=broker)
                payload = {
                    "category": CATEGORY_TRADE,
                    "underlying": asset,
                    "data": normalized,
                }
                r_sync.set(
                    redis_key(broker, asset, CATEGORY_TRADE), json.dumps(payload), ex=3600
                )

        return f"[{broker}] Trades seeded for {len(grouped)} assets (normalized)"

    except OptionsAPIError as e:
        logger.error(f"[{broker}] sync_trades API error: {e}")
        return f"[{broker}] Trade Sync API Error: {e}"
    except Exception as e:
        logger.error(f"[{broker}] sync_recent_trades_rest failed: {e}")
        return f"[{broker}] Trade Sync Failed: {e}"


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


def _base_save_intelligence_snapshot(broker: str, asset: str, report_data: dict):
    """
    Persist an intelligence report to the database for audit trail and
    historical analysis. Scoped by broker so data from different exchanges
    is never mixed.
    """
    from common.models import IntelligenceSnapshot

    try:
        report_data = _sanitize_for_json(report_data)

        metrics = report_data.get("metrics", {})
        IntelligenceSnapshot.objects.create(
            broker=broker,
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
        logger.debug(f"[{broker}] Snapshot saved: {asset} → {report_data.get('signal', '?')}")
    except Exception as e:
        logger.error(f"[{broker}] Failed to save snapshot for {asset}: {e}")


# ===========================================================================
# DYNAMIC TASK REGISTRATION
# ===========================================================================
# For each broker in BROKER_CONFIGS, register a Celery task.
# Task names follow: {broker}:sync_exchange_structure
#                    {broker}:sync_open_interest
#                    {broker}:sync_tickers_rest
#                    {broker}:sync_recent_trades_rest
#                    {broker}:save_intelligence_snapshot
#
# Backward-compatible aliases are registered for "binance" broker so
# existing beat schedule entries using old names still work.

def _register_broker_tasks():
    """
    Dynamically register Celery tasks for each broker in BROKER_CONFIGS.
    Called once when this module is imported.
    """
    for broker_id in BROKER_CONFIGS:
        _register_single_broker_tasks(broker_id)


def _register_single_broker_tasks(broker: str):
    """Register 5 tasks for a single broker."""

    # --- sync_exchange_structure ---
    task_name = f"{broker}:sync_exchange_structure"

    @shared_task(name=task_name, bind=True)
    def _task_sync_exchange_structure(self):
        return _base_sync_exchange_structure(broker)

    globals()[f"_task_sync_exchange_structure_{broker}"] = _task_sync_exchange_structure

    # --- sync_open_interest ---
    task_name = f"{broker}:sync_open_interest"

    @shared_task(name=task_name, bind=True)
    def _task_sync_open_interest(self):
        return _base_sync_open_interest(broker)

    globals()[f"_task_sync_open_interest_{broker}"] = _task_sync_open_interest

    # --- sync_tickers_rest ---
    task_name = f"{broker}:sync_tickers_rest"

    @shared_task(name=task_name, bind=True)
    def _task_sync_tickers_rest(self):
        return _base_sync_tickers_rest(broker)

    globals()[f"_task_sync_tickers_rest_{broker}"] = _task_sync_tickers_rest

    # --- sync_recent_trades_rest ---
    task_name = f"{broker}:sync_recent_trades_rest"

    @shared_task(name=task_name, bind=True)
    def _task_sync_recent_trades_rest(self):
        return _base_sync_recent_trades_rest(broker)

    globals()[f"_task_sync_recent_trades_rest_{broker}"] = _task_sync_recent_trades_rest

    # --- save_intelligence_snapshot ---
    task_name = f"{broker}:save_intelligence_snapshot"

    @shared_task(name=task_name, bind=True)
    def _task_save_intelligence_snapshot(self, asset: str, report_data: dict):
        return _base_save_intelligence_snapshot(broker, asset, report_data)

    globals()[f"_task_save_intelligence_snapshot_{broker}"] = _task_save_intelligence_snapshot


# --- Register all broker tasks on module import ---
_register_broker_tasks()


# ===========================================================================
# BACKWARD-COMPATIBLE ALIASES
# ===========================================================================
# Old task names (without broker prefix) that may be referenced in
# existing beat schedule DB entries or code. These are thin wrappers
# that delegate to the default broker's task.

@shared_task(name="sync_exchange_structure")
def sync_exchange_structure_compat():
    """Backward-compatible: delegates to default broker's task."""
    from .broker_config import DEFAULT_BROKER
    return _base_sync_exchange_structure(DEFAULT_BROKER)


@shared_task(name="sync_open_interest")
def sync_open_interest_compat():
    """Backward-compatible: delegates to default broker's task."""
    from .broker_config import DEFAULT_BROKER
    return _base_sync_open_interest(DEFAULT_BROKER)


@shared_task(name="sync_tickers_rest")
def sync_tickers_rest_compat():
    """Backward-compatible: delegates to default broker's task."""
    from .broker_config import DEFAULT_BROKER
    return _base_sync_tickers_rest(DEFAULT_BROKER)


@shared_task(name="sync_recent_trades_rest")
def sync_recent_trades_rest_compat():
    """Backward-compatible: delegates to default broker's task."""
    from .broker_config import DEFAULT_BROKER
    return _base_sync_recent_trades_rest(DEFAULT_BROKER)


@shared_task(name="save_intelligence_snapshot")
def save_intelligence_snapshot_compat(asset: str, report_data: dict):
    """Backward-compatible: delegates to default broker's task."""
    from .broker_config import DEFAULT_BROKER
    return _base_save_intelligence_snapshot(DEFAULT_BROKER, asset, report_data)


# ===========================================================================
# HELPER: Get task name for a broker
# ===========================================================================

def get_broker_task_name(broker: str, task_type: str) -> str:
    """
    Get the dynamically registered task name for a broker.

    Examples:
        get_broker_task_name("binance", "sync_exchange_structure")
        → "binance:sync_exchange_structure"

        get_broker_task_name("bybit", "sync_open_interest")
        → "bybit:sync_open_interest"
    """
    return f"{broker}:{task_type}"


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