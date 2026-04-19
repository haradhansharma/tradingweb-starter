"""
Options Streamer — Unified Multi-Broker (Management Command)
=============================================================
Django management command to start Options WebSocket streamers for ALL
brokers registered in BROKER_CONFIGS.

Architecture (Option B — Single-Process Async Multiplexing):
  - One process, one event loop, N brokers
  - For each broker, creates an OptionsConnector(broker=broker_id)
  - Uses asyncio.gather() to multiplex all brokers' streams concurrently
  - Container count stays fixed at 1 regardless of broker count
  - Adding a broker: add to BROKER_CONFIGS → restart container → done

Replaces: binance_streamer.py (kept as backward-compatible alias)

Signal handling:
  - SIGTERM/SIGINT → cancel ALL broker tasks, close ALL connections
  - Graceful shutdown with timeout (no dangling coroutines)
"""

import signal
import asyncio
import logging
from django.core.management.base import BaseCommand

from common.broker_config import BROKER_CONFIGS
from common.binance_connector import OptionsConnector
from common.assets import get_assets_sync
from django.conf import settings

logger = logging.getLogger("options.streamer")

SHUTDOWN_TIMEOUT_SEC = 5


class Command(BaseCommand):
    help = "Start Options WebSocket streamers for ALL registered brokers"

    def add_arguments(self, parser):
        parser.add_argument(
            "--brokers",
            type=str,
            default=None,
            help="Comma-separated list of brokers to stream (default: all from BROKER_CONFIGS)",
        )
        parser.add_argument(
            "--broker",
            type=str,
            default=None,
            help="Single broker to stream (shorthand for --brokers)",
        )

    def handle(self, *args, **options):
        import redis

        r = redis.from_url(
            settings.CACHES["default"]["LOCATION"], decode_responses=True
        )

        # Determine which brokers to stream
        if options.get("broker"):
            target_brokers = [options["broker"].lower()]
        elif options.get("brokers"):
            target_brokers = [b.strip().lower() for b in options["brokers"].split(",")]
        else:
            target_brokers = list(BROKER_CONFIGS.keys())

        # Validate broker IDs
        for broker_id in target_brokers:
            if broker_id not in BROKER_CONFIGS:
                self.stderr.write(
                    self.style.ERROR(f"Unknown broker '{broker_id}'. Available: {list(BROKER_CONFIGS.keys())}")
                )
                return

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting Options Streamer for brokers: {target_brokers}"
            )
        )

        # Create event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Track ALL tasks across all brokers for signal handling
        all_tasks = []
        connectors = []

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
            for task in all_tasks:
                if not task.done():
                    loop.call_soon_threadsafe(task.cancel)

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        async def run_all_brokers():
            """Create connector + stream tasks for each broker and gather them."""
            broker_coroutines = []

            for broker_id in target_brokers:
                underlyings = get_assets_sync(r, broker=broker_id)

                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [{broker_id}] Streaming {len(underlyings)} assets: {underlyings}"
                    )
                )

                connector = OptionsConnector(broker=broker_id)
                connectors.append(connector)

                # Each broker gets market + public streams (4 tasks per broker)
                async def broker_streamer(conn, underlys, bid):
                    t1 = asyncio.create_task(conn.start_market_streams(underlys))
                    t2 = asyncio.create_task(conn.start_public_streams(underlys))
                    all_tasks.extend([t1, t2])
                    await asyncio.gather(t1, t2)

                broker_coroutines.append(broker_streamer(connector, underlyings, broker_id))

            # Run all brokers concurrently
            await asyncio.gather(*broker_coroutines)

        try:
            loop.run_until_complete(run_all_brokers())
        except asyncio.CancelledError:
            self.stdout.write(self.style.WARNING("All broker stream tasks cancelled by signal."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Streamer crashed: {e}"))
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

            # Close ALL connectors
            for connector in connectors:
                try:
                    loop.run_until_complete(connector.stop())
                except RuntimeError:
                    pass

            loop.close()
            self.stdout.write(self.style.SUCCESS("All broker streamers shutdown complete."))
