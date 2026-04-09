# backend/config/asgi.py

"""
ASGI Configuration
==================
Django ASGI application with Channels WebSocket support.

Auth flow:
  - HTTP requests: standard Django middleware chain handles session cookies
  - WebSocket requests: AuthMiddlewareStack wraps the consumer, allowing
    scope["user"] to be populated. The consumer also supports JWT auth
    via query parameter as a secondary mechanism.
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
