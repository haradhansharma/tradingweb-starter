"""
Market Sessions Utility
========================
Computes which financial market sessions are currently active based on UTC time.
Designed for use in strategies and market decision-making.

Session Definitions (constant core hours in UTC):
  ┌──────────────┬──────────────────────┬──────────────────────────────────────┐
  │ Session      │ Core Hours (UTC)     │ Notes                                │
  ├──────────────┼──────────────────────┼──────────────────────────────────────┤
  │ Asian        │ 00:00 – 06:00        │ Tokyo/Sydney                         │
  │ European     │ 07:00 – 16:00        │ London (fixed UTC, ±1h DST margin)   │
  │ US           │ 14:30 – 21:00        │ New York                             │
  └──────────────┴──────────────────────┴──────────────────────────────────────┘

Data Contract (published via WS and Pub/Sub):
    {
        "sessions": {
            "asian":     { "active": true,  "label": "AS", "color": "violet" },
            "european":  { "active": false, "label": "EU", "color": "sky" },
            "us":        { "active": false, "label": "US", "color": "amber" },
        },
        "active": ["AS"],
        "label": "AS",
        "utc_hour": 2.5,
    }
"""

import datetime
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger("market.sessions")

# ---------------------------------------------------------------------------
# Session definitions — constant UTC hours (no DST, no extended windows)
# ---------------------------------------------------------------------------

SESSIONS = {
    "asian": {
        "label": "AS",
        "full_label": "Asian",
        "open_utc": 0.0,    # 00:00
        "close_utc": 6.0,   # 06:00
        "color": "violet",
        "weight": 1,
    },
    "european": {
        "label": "EU",
        "full_label": "European",
        "open_utc": 7.0,    # 07:00 London (±1h DST tolerance baked in)
        "close_utc": 16.0,  # 16:00 London
        "color": "sky",
        "weight": 2,
    },
    "us": {
        "label": "US",
        "full_label": "US",
        "open_utc": 14.5,   # 14:30 NYSE
        "close_utc": 21.0,  # 21:00
        "color": "amber",
        "weight": 2,
    },
}


def _hour_minute_utc() -> float:
    """Return current UTC time as decimal hours (e.g., 14:30 → 14.5)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.hour + now.minute / 60.0


def compute_sessions(utc_hour: float = None) -> Dict[str, Any]:
    """
    Compute all market session states for the current (or given) UTC hour.

    Returns the full data contract dict consumed by frontend and strategies.
    """
    if utc_hour is None:
        utc_hour = _hour_minute_utc()

    sessions: Dict[str, Any] = {}
    active_labels: List[str] = []

    for key, cfg in SESSIONS.items():
        is_active = cfg["open_utc"] <= utc_hour < cfg["close_utc"]

        sessions[key] = {
            "active": is_active,
            "label": cfg["label"],
            "color": cfg["color"],
            "weight": cfg["weight"],
        }

        if is_active:
            active_labels.append(cfg["label"])

    # Primary display label: highest weight active session
    display_label = ""
    display_color = "gray"
    max_weight = 0

    for key, s in sessions.items():
        if s["active"] and s["weight"] > max_weight:
            max_weight = s["weight"]
            display_label = s["label"]
            display_color = s["color"]

    # If multiple active, combine labels (e.g., "EU+US")
    if len(active_labels) > 1:
        display_label = "+".join(active_labels)

    return {
        "sessions": sessions,
        "active": active_labels,
        "label": display_label,
        "color": display_color,
        "utc_hour": round(utc_hour, 2),
        "weight": max_weight,
    }
