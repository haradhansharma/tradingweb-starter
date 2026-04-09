"""
Common App — MarketPulse Trading Data Pipeline
===============================================
Broker-agnostic data ingestion, normalization, and intelligence calculation.

Public API (Connector Factory):
    get_options_connector(broker) → OptionsConnector
    get_futures_connector(broker) → FuturesConnector
    get_options_rest_client(broker) → OptionsRESTClient
    get_futures_rest_client(broker) → FuturesRESTClient

Adding a new broker:
    1. Add entry to broker_config.BROKER_CONFIGS
    2. Create {broker}_connector.py if WS protocol differs (optional)
    3. Create {broker}_rest.py if REST API differs (optional)
    4. Register normalizer in normalizers._BROKER_NORMALIZERS (if field names differ)
    5. That's it — tasks, beat schedule, streamers auto-adapt
"""

from .broker_config import (
    BROKER_CONFIGS,
    DEFAULT_BROKER,
    get_broker_config,
    get_available_brokers,
    redis_key,
    redis_global_key,
    ws_group_name,
    ws_global_group,
    ws_universal_group,
    pubsub_channel,
)


# ---------------------------------------------------------------------------
# Connector Factory Functions
# ---------------------------------------------------------------------------

def get_options_connector(broker: str = DEFAULT_BROKER, **kwargs):
    """
    Factory: Get the Options WebSocket connector for a broker.

    All connectors read their WS URLs from broker_config.py.
    Currently, all brokers share the same OptionsConnector class
    (which is protocol-agnostic). If a broker needs a custom protocol
    handler, register it here.

    Parameters
    ----------
    broker : str    Broker identifier (e.g. "binance", "bybit")
    **kwargs        Passed to OptionsConnector (e.g. testnet=True)

    Returns
    -------
    OptionsConnector instance
    """
    from .binance_connector import OptionsConnector
    return OptionsConnector(broker=broker, **kwargs)


def get_futures_connector(broker: str = DEFAULT_BROKER, **kwargs):
    """
    Factory: Get the Futures WebSocket connector for a broker.

    Parameters
    ----------
    broker : str    Broker identifier
    **kwargs        Passed to FuturesConnector

    Returns
    -------
    FuturesConnector instance
    """
    from .futures_connector import FuturesConnector
    return FuturesConnector(broker=broker, **kwargs)


def get_options_rest_client(broker: str = DEFAULT_BROKER, **kwargs):
    """
    Factory: Get the Options REST client for a broker.

    Parameters
    ----------
    broker : str    Broker identifier
    **kwargs        Passed to OptionsRESTClient (e.g. testnet=True)

    Returns
    -------
    OptionsRESTClient instance
    """
    from .binance_rest import OptionsRESTClient
    return OptionsRESTClient(broker=broker, **kwargs)


def get_futures_rest_client(broker: str = DEFAULT_BROKER, **kwargs):
    """
    Factory: Get the Futures REST client for a broker.

    Parameters
    ----------
    broker : str    Broker identifier
    **kwargs        Passed to FuturesRESTClient

    Returns
    -------
    FuturesRESTClient instance
    """
    from .futures_rest import FuturesRESTClient
    return FuturesRESTClient(broker=broker, **kwargs)
