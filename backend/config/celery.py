# backend/config/celery.py
"""
Celery Configuration
=====================
Celery app setup with beat schedule, task tracking, and worker settings.

Fixes applied:
  - task_acks_late for crash recovery
  - task_reject_on_worker_lost to prevent zombie tasks
  - Beat schedule intervals documented
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
# Beat Schedule
# ---------------------------------------------------------------------------
app.conf.beat_schedule = {
    # Structural data: changes infrequently, sync once per hour
    "sync_exchange_structure_1h": {
        "task": "sync_exchange_structure",
        "schedule": 3600.0,
    },
    # Open Interest: moderate frequency, 5-minute REST sync
    "sync_open_interest_5m": {
        "task": "sync_open_interest",
        "schedule": 300.0,
    },
    # Tickers: 30-second REST sync (supplements WS stream as fallback seed)
    "sync_tickers_30s": {
        "task": "sync_tickers_rest",
        "schedule": 30.0,
    },
    # Block Trades: 1-minute REST sync (seeds trade cache on startup)
    "sync_trades_1m": {
        "task": "sync_recent_trades_rest",
        "schedule": 60.0,
    },
}


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
