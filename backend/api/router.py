# backend/api/router.py
from ninja import NinjaAPI, Router
import json
from django.http import HttpResponse, JsonResponse
import redis.asyncio as aioredis
from django.conf import settings

from apps.common.assets import get_assets_async
from apps.common.indicator_engine import IndicatorEngine

api = NinjaAPI(
    title="WebTrading Options Intelligence API",
    version="2.0.0",
    description="Real-time Futures Trading Signals based on Options Data (7-Variable Framework)",
)

router = Router()

# --- SINGLETON PATTERN ---
# Persistent pool for all users. Do NOT call aclose() on this inside endpoints.
_redis_client = aioredis.from_url(
    settings.CACHES["default"]["LOCATION"],
    encoding="utf-8",
    decode_responses=True,
    max_connections=50
)

def get_redis_client():
    return _redis_client

# =============================================================================
# HEALTH CHECK
# =============================================================================

@api.get("/health")
async def health_check(request):
    r = get_redis_client()
    try:
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": redis_ok,
        "framework": "7-Variable Analysis"
    }

# =============================================================================
# MARKET DATA ROUTERS
# =============================================================================

@router.get("/active-underlyings")
async def active_underlyings(request):
    r = get_redis_client()
    candidates = await get_assets_async(r)

    # Validate: only return assets with BOTH OI data AND mark price data.
    # Some underlyings (e.g. XRPUSDT, DOGEUSDT) may have stale OI from a
    # previous sync but no live mark price stream from Binance — meaning
    # no options are actively tradeable. Requiring both ensures only truly
    # active assets reach the frontend.
    # On first run (before sync completes), fall back to all candidates.
    if candidates:
        oi_keys = [f"binance:{asset}:open_interest" for asset in candidates]
        mp_keys = [f"binance:{asset}:markPrice" for asset in candidates]
        oi_values = await r.mget(oi_keys)
        mp_values = await r.mget(mp_keys)
        validated = [
            a for a, oi, mp in zip(candidates, oi_values, mp_values)
            if oi and mp
        ]
        if validated:
            candidates = validated

    return {
        "active_underlyings": candidates,
        "engine": "7-Variable Reactive Analyzer",
        "db_status": "connected"
    }


@router.get("/live/{underlying}/{category}")
async def get_live_data(request, underlying: str, category: str):
    """
    High-speed pipe for MarkPrice, Ticker, Index, and Trade.
    Bypasses JSON parsing to serve data in sub-milliseconds.
    """
    r = get_redis_client()
    key = f"binance:{underlying.upper()}:{category}"

    # Fetch the raw string from Redis
    data = await r.get(key)

    if not data:
        return HttpResponse('{"error": "No data found"}', content_type="application/json", status=404)

    # Return raw string directly to frontend
    return HttpResponse(data, content_type="application/json")

@router.get("/open-interest/{underlying}")
async def get_oi(request, underlying: str, expiration: str = None):
    """Fetches Open Interest from Cache."""
    r = get_redis_client()
    key = f"binance:{underlying.upper()}:open_interest"
    if expiration:
        key += f":{expiration}"

    data = await r.get(key)
    if not data:
        return {"error": "OI data not found. Ensure sync task is running."}

    return HttpResponse(data, content_type="application/json")

# =============================================================================
# INTELLIGENCE ROUTER (The Flagship)
# =============================================================================

@router.get("/intelligence/{underlying}")
async def get_intelligence(request, underlying: str):
    """
    Returns the pre-calculated 7-Variable Intelligence Report.
    This is the core of your '7-Variable Framework'.
    """
    r = get_redis_client()
    data = await r.get(f"binance:{underlying.upper()}:intelligence")

    if not data:
        return {
            "status": "calculating",
            "message": "Intelligence engine is warming up or waiting for next MarkPrice tick."
        }

    return HttpResponse(data, content_type="application/json")

# =============================================================================
# INDICATOR CONFIG ROUTER (Dynamic Rendering)
# =============================================================================

@router.get("/indicator-config")
async def indicator_config(request):
    """
    Returns the full indicator display configuration for the frontend.
    The frontend uses this to dynamically render all indicator rows
    without any hardcoded HTML. Add a new indicator to INDICATOR_REGISTRY
    in indicator_engine.py and it appears here automatically.
    """
    config = IndicatorEngine.get_display_config()
    return config

# Register the router under the /market prefix
api.add_router("/market", router)
