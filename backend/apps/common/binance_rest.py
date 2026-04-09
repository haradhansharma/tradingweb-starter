# # backend/apps/common/binance_rest.py
"""
Options REST Client
===================
Async HTTP client for broker Options API.
Reads all URLs, settings, and paths from broker_config.py — no hardcoded values.

Includes:
  - HTTP status code checking (429, 418, 5xx → raise, don't return error JSON)
  - Explicit timeout configuration
  - Automatic retry with exponential backoff for transient errors
  - HMAC SHA256 signature for authenticated endpoints
"""

import time
import hmac
import hashlib
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlencode

import httpx

from .broker_config import (
    get_broker_config,
    get_broker_settings,
    get_non_retryable_codes,
    DEFAULT_BROKER,
)

logger = logging.getLogger("options.rest")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 1.0  # seconds, doubles each retry


class OptionsAPIError(Exception):
    """Raised when the broker Options API returns an error response."""

    def __init__(self, status_code: int, data: Any, broker: str = "unknown"):
        self.status_code = status_code
        self.data = data
        self.broker = broker
        msg = data.get("msg", str(data)) if isinstance(data, dict) else str(data)
        super().__init__(f"{broker.title()} Options API Error {status_code}: {msg}")


# Backward-compatible alias
BinanceAPIError = OptionsAPIError


class OptionsRESTClient:
    """
    Broker-agnostic Options REST client.
    All URLs, API paths, and auth headers are read from broker_config.py.
    """

    def __init__(
        self,
        broker: str = DEFAULT_BROKER,
        testnet: bool = False,
        api_key: str = None,
        api_secret: str = None,
    ):
        self.broker = broker
        self.testnet = testnet

        config = get_broker_config(broker)
        options = config["options"]

        # API credentials — explicit params override broker_config settings
        broker_settings = get_broker_settings(broker)
        self.api_key = api_key or broker_settings["api_key"]
        self.api_secret = api_secret or broker_settings["api_secret"]
        self.auth_header = broker_settings.get("auth_header", "X-MBX-APIKEY")

        # Non-retryable HTTP codes from broker config
        self._non_retryable = get_non_retryable_codes(broker)

        # Base URL — testnet vs production
        if testnet:
            self.base_url = options.get("rest_base_url_testnet", options["rest_base_url"])
        else:
            self.base_url = options["rest_base_url"]

        # API paths from config
        self._paths = {
            "exchange_info": options.get("exchange_info_path", "/eapi/v1/exchangeInfo"),
            "open_interest": options.get("open_interest_path", "/eapi/v1/openInterest"),
            "ticker": options.get("ticker_path", "/eapi/v1/ticker"),
            "trades": options.get("trades_path", "/eapi/v1/trades"),
            "block_trades": options.get("block_trades_path", "/eapi/v1/blockTrades"),
            "account": options.get("account_path", "/eapi/v1/account"),
        }

        # Explicit timeout + retry transport
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=HTTP_TIMEOUT,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    def _generate_signature(self, query_string: str) -> str:
        """HMAC SHA256 signature per broker docs."""
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        params: Optional[Dict] = None,
        _retry_count: int = 0,
    ) -> Any:
        """
        Internal request wrapper with error handling and retry logic.
        """
        params = params or {}
        headers = {
            self.auth_header: self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query_string = urlencode(params)
            params["signature"] = self._generate_signature(query_string)

        try:
            response = await self.client.request(
                method, path, params=params, headers=headers
            )
        except httpx.TimeoutException as e:
            logger.error(f"Timeout on {method} {path}: {e}")
            raise

        # --- HTTP Status Checking ---
        if response.status_code in self._non_retryable:
            data = response.json()
            logger.error(
                f"{self.broker.title()} non-retryable error "
                f"{response.status_code}: {data}"
            )
            raise OptionsAPIError(response.status_code, data, self.broker)

        if response.status_code >= 500 and _retry_count < MAX_RETRIES:
            backoff = RETRY_BACKOFF_BASE * (2**_retry_count)
            logger.warning(
                f"{self.broker.title()} 5xx {response.status_code} on "
                f"{method} {path}. Retry {_retry_count + 1}/{MAX_RETRIES} "
                f"in {backoff}s"
            )
            import asyncio

            await asyncio.sleep(backoff)
            return await self._request(method, path, signed, params, _retry_count + 1)

        if response.status_code >= 400:
            data = response.json()
            logger.error(
                f"{self.broker.title()} client error {response.status_code}: {data}"
            )
            raise OptionsAPIError(response.status_code, data, self.broker)

        return response.json()

    # ------------------------------------------------------------------
    # Public Endpoints (data fetching)
    # ------------------------------------------------------------------

    async def get_exchange_info(self):
        return await self._request("GET", self._paths["exchange_info"])

    async def get_open_interest(self, underlying: str, expiration: str):
        return await self._request(
            "GET",
            self._paths["open_interest"],
            params={"underlyingAsset": underlying, "expiration": expiration},
        )

    async def get_ticker(self, symbol: str = None):
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", self._paths["ticker"], params=params)

    async def get_recent_trades(self, symbol: str, limit: int = 100):
        return await self._request(
            "GET",
            self._paths["trades"],
            params={"symbol": symbol, "limit": limit},
        )

    async def get_block_trades(self, symbol: str = None, limit: int = 50):
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        return await self._request("GET", self._paths["block_trades"], params=params)

    async def get_account_info(self):
        return await self._request("GET", self._paths["account"], signed=True)


# Backward-compatible alias — existing code uses BinanceRESTClient
BinanceRESTClient = OptionsRESTClient
