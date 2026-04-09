"""
Binance Streamer — Management Command
======================================
Django management command to start the Binance Options WebSocket streamer.
Designed to run inside a Docker container.

Fixes applied:
  - Proper signal handling (SIGTERM, SIGINT) for graceful shutdown
  - Tasks are cancelled before loop closes (no dangling coroutines)
  - Cleanup timeout prevents infinite hang on shutdown
  - Clean resource cleanup
"""

import signal
import asyncio
import logging
from django.core.management.base import BaseCommand

from apps.common.binance_connector import OptionsConnector
from apps.common.assets import get_assets_sync
from apps.common.broker_config import DEFAULT_BROKER
from django.conf import settings

logger = logging.getLogger("options.streamer")

# How long to wait for tasks to clean up after cancellation before force-closing
SHUTDOWN_TIMEOUT_SEC = 5


class Command(BaseCommand):
    help = "Start Binance Options WebSocket streamer for top N assets"

    def handle(self, *args, **options):
        # 1. Read the top underlyings list from Redis (sync — before event loop)
        import redis

        r = redis.from_url(
            settings.CACHES["default"]["LOCATION"], decode_responses=True
        )

        underlyings = get_assets_sync(r)

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting Streamer for Top {len(underlyings)} Assets: {underlyings}"
            )
        )

        # 2. Create connector
        connector = OptionsConnector(broker=DEFAULT_BROKER)

        # 3. Create event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 4. Store the stream tasks so the signal handler can cancel them
        stream_tasks = []

        # signal.signal() passes (signum: int, frame), not a Signals enum
        def _signal_handler(signum, frame):
            sig_name = (
                signal.Signals(signum).name
                if hasattr(signal, "Signals")
                else str(signum)
            )
            self.stdout.write(
                self.style.WARNING(
                    f"Received signal {sig_name} ({signum}). Initiating graceful shutdown..."
                )
            )
            # Cancel all running stream tasks from within the loop
            for task in stream_tasks:
                if not task.done():
                    loop.call_soon_threadsafe(task.cancel)

        # Register signal handlers on the main thread
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        async def run_streams():
            """Wrap both streams as tasks so they can be individually cancelled."""
            t1 = asyncio.create_task(connector.start_market_streams(underlyings))
            t2 = asyncio.create_task(connector.start_public_streams(underlyings))
            stream_tasks.extend([t1, t2])
            # Wait for both — if one is cancelled, the other keeps running
            # but the gather will propagate the CancelledError up
            await asyncio.gather(t1, t2)

        try:
            loop.run_until_complete(run_streams())
        except asyncio.CancelledError:
            self.stdout.write(self.style.WARNING("Stream tasks cancelled by signal."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Streamer crashed: {e}"))
        finally:
            # Give pending tasks a brief window to finish cleanup
            self.stdout.write("Cleaning up connections...")
            pending = asyncio.all_tasks(loop)
            for task in pending:
                if not task.done():
                    task.cancel()
            # Wait for cancellations to propagate (with timeout)
            if pending:
                try:
                    loop.run_until_complete(
                        asyncio.wait(pending, timeout=SHUTDOWN_TIMEOUT_SEC)
                    )
                except RuntimeError:
                    pass  # Loop already closing
            # Close the bridge BEFORE closing the loop
            try:
                loop.run_until_complete(connector.stop())
            except RuntimeError:
                pass  # Loop already closed — best effort
            loop.close()
            self.stdout.write(self.style.SUCCESS("Shutdown complete."))
