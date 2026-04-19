# backend/apps/common/apps.py
from django.apps import AppConfig
import logging

logger = logging.getLogger("common.apps")

class CommonConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'common'
    
    def ready(self):
        """Flush Redis cache (DB0) on application startup.

        Ensures a clean slate every time the app starts — no stale
        mark prices, indicators, kline data, or OI snapshots from
        previous sessions. History is re-seeded by REST tasks and
        the futures orchestrator's seed_from_rest().
        """
 
        # try:
        #     from django.core.cache import cache
        #     cache.clear()
        #     logger.info("Flushed Redis DB0 (default cache) on startup")
        # except Exception as e:
        #     logger.warning(f"Failed to flush cache on startup: {e}")
