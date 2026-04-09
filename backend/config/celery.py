# backend/config/celery.py
"""
Celery Configuration
=====================
Celery app setup with dynamic beat schedule that auto-generates tasks
for every broker registered in BROKER_CONFIGS.

Architecture:
  - Beat schedule is built dynamically from BROKER_CONFIGS at startup
  - Each broker gets 4 periodic tasks: exchange_structure, open_interest,
    tickers, trades
  - Task names follow: {broker}:{task_type} (e.g. "binance:sync_open_interest")
  - Adding a new broker: just add entry to BROKER_CONFIGS — beat schedule
    auto-adapts. No changes needed here.

Fixes applied:
  - task_acks_late for crash recovery
  - task_reject_on_worker_lost to prevent zombie tasks
  - Dynamic beat schedule from BROKER_CONFIGS (not hardcoded)
"""

from __future__ import absolute_import, unicode_literals
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("tradingweb")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# ---------------------------------------------------------------------------
# Task Tracking & Reliability
# ---------------------------------------------------------------------------
app.conf.task_track_started = True
app.conf.worker_send_task_events = True

# Crash recovery: if a worker dies mid-task, the task is retried
# instead of being lost or stuck in STARTED state forever
app.conf.task_acks_late = True
app.conf.task_reject_on_worker_lost = True

# Default retry settings for shared tasks
app.conf.task_default_retry_delay = 30   # seconds
app.conf.task_max_retries = 3


# ---------------------------------------------------------------------------
# Dynamic Beat Schedule Builder
# ---------------------------------------------------------------------------
# Schedule intervals per task type (in seconds).
# These apply to ALL brokers uniformly.
TASK_SCHEDULES = {
    "sync_exchange_structure": 3600.0,    # 1 hour — structural data changes infrequently
    "sync_open_interest": 300.0,           # 5 minutes — moderate frequency
    "sync_tickers_rest": 30.0,             # 30 seconds — supplements WS stream
    "sync_recent_trades_rest": 60.0,       # 1 minute — seeds trade cache on startup
}


def build_beat_schedule():
    """
    Dynamically build the beat schedule from BROKER_CONFIGS.

    For each registered broker, creates 4 scheduled entries:
      - {broker}_sync_exchange_structure_1h
      - {broker}_sync_open_interest_5m
      - {broker}_sync_tickers_30s
      - {broker}_sync_trades_1m

    Task names match the dynamically registered tasks in tasks.py:
      {broker}:sync_exchange_structure
      {broker}:sync_open_interest
      {broker}:sync_tickers_rest
      {broker}:sync_recent_trades_rest

    Returns:
        dict suitable for app.conf.beat_schedule
    """
    from apps.common.broker_config import BROKER_CONFIGS

    schedule = {}

    for broker_id in BROKER_CONFIGS:
        for task_type, interval in TASK_SCHEDULES.items():
            schedule_key = f"{broker_id}_{task_type}"
            schedule[schedule_key] = {
                "task": f"{broker_id}:{task_type}",
                "schedule": interval,
            }

    return schedule


# Build and set the dynamic beat schedule
# This runs when celery.py is imported (beat startup)
app.conf.beat_schedule = build_beat_schedule()


# from __future__ import absolute_import, unicode_literals
# import os
# from celery import Celery
# from celery.schedules import crontab


# os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# app = Celery("tradingweb")
# app.config_from_object("django.conf:settings", namespace="CELERY")
# app.autodiscover_tasks()

# # Task tracking
# app.conf.task_track_started = True
# app.conf.worker_send_task_events = True


# app.conf.beat_schedule = {
#     "sync_exchange_structure_24": {
#         "task": "sync_exchange_structure",
#         "schedule": 360.0, # 1 Hour (Structural data doesn't change every 36s)
#     },
#     # Daily exchange info refresh
#     "sync_open_interest_5m": {
#         "task": "sync_open_interest",
#         "schedule": 300.0, # 5 Minutes
#     },
#     "sync_tickers_every_30s": {
#         "task": "sync_tickers_rest",
#         "schedule": 30.0, # 30 Seconds is fine
#     },
#     "sync_trades_every_1m": {
#         "task": "sync_recent_trades_rest",
#         "schedule": 60.0, # 1 Minute - Trades are high frequency but we want to avoid hitting rate limits with REST
#     },
# }



# app.conf.beat_schedule = {
    
#     # High-frequency analysis (every 5 seconds)
#     "scheduled-analysis-every-5s": {
#         "task": "scheduled_analysis",
#         "schedule": 5.0,
#         "options": {"queue": "default"},
#     },
#     # Daily exchange info refresh (at midnight UTC)
#     "refresh-exchange-info-daily": {
#         "task": "refresh_exchange_info",
#         "schedule": crontab(hour=0, minute=0),
#         "options": {"queue": "default"},
#     },
    
    # "update-market-snapshot-every-60s": {
    #     "task": "update_market_snapshot",
    #     "schedule": 60.0,  # Every 60 seconds - low frequency data
    # },
    # "update-intelligence-every-5s": {
    #     "task": "update_intelligence_fast",
    #     "schedule": 5.0,  # Every 5 seconds - high frequency block trades
    # },
    # 'task-every-30-minutes': {
    #     'task': 'myapp.tasks.some_task',w
    #     'schedule': 1800.0,  # 30 minutes
    # },
    # 'task-every-5-seconds': {
    #     'task': 'myapp.tasks.some_task',
    #     'schedule': 5.0,  # 5 seconds
    # },
    # 'task-at-sunrise': {
    #     'task': 'myapp.tasks.some_task',
    #     'schedule': solar('sunrise', latitude=40.7128, longitude=-74.0060),
    # },
    # 'task-at-sunset': {
    #     'task': 'myapp.tasks.some_task',
    #     'schedule': solar('sunset', latitude=40.7128, longitude=-74.0060),
    # },
    # 'custom-schedule-task': {
    #     'task': 'myapp.tasks.some_task',
    #     'schedule': timedelta(days=1, hours=2, minutes=30),  # Every 1 day, 2 hours, and 30 minutes
    # },
# }

# # =============================================================================
# # STARTUP TASK
# # =============================================================================

# @app.on_after_configure.connect
# def setup_initial_tasks(sender, **kwargs):
#     """
#     Setup initial tasks when Celery is configured.
#     Note: This only runs when the beat scheduler starts.
#     """
#     # The websocket_manager task should be started manually or via
#     # a separate initialization process, not via beat schedule.
#     pass
