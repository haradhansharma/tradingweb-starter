"""
Binance Options Streamer — Backward-Compatible Alias
=====================================================
This command is preserved for backward compatibility with existing
docker-compose configurations. It delegates to the unified
options_streamer command with --broker=binance.

Prefer using `python manage.py options_streamer` directly for
multi-broker support.
"""

from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command


class Command(BaseCommand):
    help = "[LEGACY] Start Binance Options WebSocket streamer. Prefer: python manage.py options_streamer --broker binance"

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.WARNING(
                "This command is a legacy alias. Use 'python manage.py options_streamer --broker binance' instead."
            )
        )
        call_command("options_streamer", broker="binance")