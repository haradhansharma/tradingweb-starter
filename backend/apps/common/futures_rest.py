# backend/apps/common/futures_rest.py
"""
Futures REST Client
===================
Async HTTP client for broker USDM Futures public endpoints.
Reads all URLs and config from broker_config.py — no hardcoded values.

Used to seed historical kline data before WS takes over real-time updates.

NOTE: Only PUBLIC endpoints (no auth required). The kline endpoint
does not need API key/secret. Keeping this separate from the Options
REST client (Options REST) to maintain clean separation.
"""

import logging
from typing import Optional, Dict, Any

import httpx

from .broker_config import (
    get_broker_config,
    get_non_retryable_codes,
    DEFAULT_BROKER,
)

logger = logging.getLogger("futures.rest")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 1.0


class FuturesAPIError(Exception):
    """Raised when the broker Futures API returns an error response."""

    def __init__(self, status_code: int, data: Any, broker: str = "unknown"):
        self.status_code = status_code
        self.data = data
        self.broker = broker
        msg = data.get("msg", str(data)) if isinstance(data, dict) else str(data)
        super().__init__(f"{broker.title()} Futures API Error {status_code}: {msg}")


class FuturesRESTClient:
    """
    Broker-agnostic Futures REST client for historical kline seeding.
    All URLs and config are read from broker_config.py.
    Only used for historical kline seeding — no auth required.
    """

    def __init__(self, broker: str = DEFAULT_BROKER):
        self.broker = broker
        config = get_broker_config(broker)
        futures_config = config["futures"]

        self.base_url = futures_config["rest_base_url"]
        self._kline_path = futures_config.get("kline_path", "/fapi/v1/klines")
        self._non_retryable = get_non_retryable_codes(broker)

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
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

        if response.status_code in self._non_retryable:
            data = response.json()
            logger.error(
                f"Futures non-retryable error {response.status_code}: {data}"
            )
            raise FuturesAPIError(response.status_code, data, self.broker)

        if response.status_code >= 500 and _retry_count < MAX_RETRIES:
            import asyncio

            backoff = RETRY_BACKOFF_BASE * (2**_retry_count)
            logger.warning(
                f"Futures 5xx {response.status_code} on {method} {path}. "
                f"Retry {_retry_count + 1}/{MAX_RETRIES} in {backoff}s"
            )
            await asyncio.sleep(backoff)
            return await self._request(method, path, params, _retry_count + 1)

        if response.status_code >= 400:
            data = response.json()
            logger.error(f"Futures client error {response.status_code}: {data}")
            raise FuturesAPIError(response.status_code, data, self.broker)

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

        Parameters
        ----------
        symbol : str       e.g. "BTCUSDT"
        interval : str     e.g. "1m", "5m", "1h", "1d"
        limit : int        1-1500, default 500
        start_time : int   Optional start time in ms epoch
        end_time : int     Optional end time in ms epoch

        Returns
        -------
        list of lists — raw kline format
        """
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        return await self._request("GET", self._kline_path, params=params)

    async def get_exchange_info(self) -> dict:
        """Fetch futures exchange info (symbol list, tick size, etc)."""
        config = get_broker_config(self.broker)
        futures_config = config["futures"]
        path = futures_config.get("exchange_info_path", "/fapi/v1/exchangeInfo")
        return await self._request("GET", path)

