# backend/config/asgi.py

"""
ASGI Configuration
==================
Django ASGI application with Channels WebSocket support.

Note: AuthMiddlewareStack is retained for future authentication.
When consumer auth is implemented, this middleware will validate
the user from the scope and enforce subscription permissions.
"""

import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from apps.common.routing import websocket_urlpatterns

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        ),
    }
)
