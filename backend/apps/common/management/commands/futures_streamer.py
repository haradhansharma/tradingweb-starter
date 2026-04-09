"""
Futures Streamer — Management Command
=======================================
Django management command to start the Futures WebSocket streamer.
Runs alongside options_streamer.py as a separate Docker container.

This command:
  1. Reads top underlyings from Redis
  2. Seeds historical kline data via REST
  3. Starts real-time WS kline stream
  4. Calculates indicators on each completed candle
  5. Publishes results to Redis + Django Channels → Frontend

Signal handling:
  - SIGTERM/SIGINT → graceful shutdown (cancel tasks, close connections)
"""

import signal
import asyncio
import logging
from django.core.management.base import BaseCommand

from apps.common.futures_orchestrator import FuturesOrchestrator
from apps.common.assets import get_assets_sync
from apps.common.broker_config import DEFAULT_BROKER
from django.conf import settings

logger = logging.getLogger("futures.streamer")

SHUTDOWN_TIMEOUT_SEC = 5


class Command(BaseCommand):
    help = "Start Futures kline WebSocket streamer with indicator calculations"

    def handle(self, *args, **options):
        # 1. Read the top underlyings list from Redis
        import redis

        r = redis.from_url(
            settings.CACHES["default"]["LOCATION"], decode_responses=True
        )

        underlyings = get_assets_sync(r)

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting Futures Streamer for {len(underlyings)} assets: {underlyings}"
            )
        )

        # 2. Create orchestrator
        orchestrator = FuturesOrchestrator(broker=DEFAULT_BROKER)

        # 3. Create event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 4. Signal handlers
        def _signal_handler(signum, frame):
            sig_name = (
                signal.Signals(signum).name
                if hasattr(signal, "Signals")
                else str(signum)
            )
            self.stdout.write(
                self.style.WARNING(
                    f"Received signal {sig_name} ({signum}). "
                    f"Initiating graceful shutdown..."
                )
            )
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(orchestrator.stop()))

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        # 5. Run
        try:
            loop.run_until_complete(orchestrator.run(underlyings))
        except asyncio.CancelledError:
            self.stdout.write(self.style.WARNING("Futures streamer cancelled by signal."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Futures streamer crashed: {e}"))
        finally:
            self.stdout.write("Cleaning up connections...")
            pending = asyncio.all_tasks(loop)
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                try:
                    loop.run_until_complete(
                        asyncio.wait(pending, timeout=SHUTDOWN_TIMEOUT_SEC)
                    )
                except RuntimeError:
                    pass
            try:
                loop.run_until_complete(orchestrator.close())
            except RuntimeError:
                pass
            loop.close()
            self.stdout.write(self.style.SUCCESS("Futures streamer shutdown complete."))
