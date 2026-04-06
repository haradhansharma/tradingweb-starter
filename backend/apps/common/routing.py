# backend/apps/common/routing.py

"""
WebSocket URL Routing for Django Channels
"""

from django.urls import path, re_path
from .consumers import MarketConsumer

websocket_urlpatterns = [
    # Main market data WebSocket endpoint
    path("ws/market/", MarketConsumer.as_asgi()),
]



