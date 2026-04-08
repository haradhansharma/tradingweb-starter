# backend/apps/common/futures_rest.py

"""
Futures REST Client
===================
Async HTTP client for Binance USDM Futures public endpoints.
Used to seed historical kline data before WS takes over real-time updates.

NOTE: Only PUBLIC endpoints (no auth required). The kline endpoint
does not need API key/secret. Keeping this separate from binance_rest.py
(which targets the Options /eapi/v1/ API) to maintain clean separation.
"""

import logging
from typing import Optional, Dict, Any

import httpx

logger = logging.getLogger("futures.rest")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://fapi.binance.com"
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 1.0

# Binance error codes that should NOT be retried
NON_RETRYABLE_CODES = {418, 429}

# Supported kline intervals (from Binance docs)
VALID_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
}


class FuturesAPIError(Exception):
    """Raised when Binance Futures API returns an error response."""

    def __init__(self, status_code: int, data: Any):
        self.status_code = status_code
        self.data = data
        msg = data.get("msg", str(data)) if isinstance(data, dict) else str(data)
        super().__init__(f"Futures API Error {status_code}: {msg}")


class FuturesRESTClient:
    """
    Lightweight async client for Binance USDM Futures public REST API.
    Only used for historical kline seeding — no auth required.
    """

    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=HTTP_TIMEOUT,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        _retry_count: int = 0,
    ) -> Any:
        """Internal request wrapper with retry logic."""
        params = params or {}

        try:
            response = await self.client.request(method, path, params=params)
        except httpx.TimeoutException as e:
            logger.error(f"Timeout on {method} {path}: {e}")
            raise

        if response.status_code in NON_RETRYABLE_CODES:
            data = response.json()
            logger.error(f"Futures non-retryable error {response.status_code}: {data}")
            raise FuturesAPIError(response.status_code, data)

        if response.status_code >= 500 and _retry_count < MAX_RETRIES:
            import asyncio
            backoff = RETRY_BACKOFF_BASE * (2 ** _retry_count)
            logger.warning(
                f"Futures 5xx {response.status_code} on {method} {path}. "
                f"Retry {_retry_count + 1}/{MAX_RETRIES} in {backoff}s"
            )
            await asyncio.sleep(backoff)
            return await self._request(method, path, params, _retry_count + 1)

        if response.status_code >= 400:
            data = response.json()
            logger.error(f"Futures client error {response.status_code}: {data}")
            raise FuturesAPIError(response.status_code, data)

        return response.json()

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 500,
        start_time: int = None,
        end_time: int = None,
    ) -> list:
        """
        Fetch kline/candlestick data for a futures symbol.

        Endpoint: GET /fapi/v1/klines
        Docs: https://binance-docs.github.io/apidocs/futures/en/#kline-candlestick-data

        Parameters
        ----------
        symbol : str       e.g. "BTCUSDT"
        interval : str     e.g. "1m", "5m", "1h", "1d"
        limit : int        1-1500, default 500
        start_time : int   Optional start time in ms epoch
        end_time : int     Optional end time in ms epoch

        Returns
        -------
        list of lists — raw Binance kline format:
        [
            [open_time, open, high, low, close, volume, close_time,
             quote_volume, trades, taker_buy_volume, taker_buy_quote_volume, ignore]
        ]
        """
        if interval not in VALID_INTERVALS:
            raise ValueError(f"Invalid interval '{interval}'. Must be one of {VALID_INTERVALS}")

        limit = max(1, min(limit, 1500))

        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        return await self._request("GET", "/fapi/v1/klines", params=params)

    async def get_exchange_info(self) -> dict:
        """Fetch futures exchange info (symbol list, tick size, etc)."""
        return await self._request("GET", "/fapi/v1/exchangeInfo")
