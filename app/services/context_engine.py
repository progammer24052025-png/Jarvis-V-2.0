"""
Context Engine for J.A.R.V.I.S.
================================
Provides real-time context for every reasoning request.
The context is injected into the system prompt so JARVIS knows:
  - Which device the user is on
  - Current time, day of week
  - Connected devices and their capabilities
  - Current mood (from emotional service)
  - Environment info (time of day, battery, network)

This replaces hard-coded assumptions with dynamic awareness.
"""

import logging
import platform
from datetime import datetime
from typing import Optional

from config import BOSS_NAME, ASSISTANT_NAME

logger = logging.getLogger("J.A.R.V.I.S")


def _time_of_day_label(hour: int) -> str:
    """Return human-friendly time-of-day label."""
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"


class ContextEngine:
    """
    Builds a snapshot of the current context for each request.
    Thread-safe: each call to build_context() creates a fresh snapshot.
    """

    def __init__(self):
        self._device_manager = None   # set during main.py lifespan
        self._emotional_service = None  # set during main.py lifespan

    def set_device_manager(self, dm) -> None:
        """Wire up the device manager (called at startup)."""
        self._device_manager = dm

    def set_emotional_service(self, es) -> None:
        """Wire up the emotional service (called at startup)."""
        self._emotional_service = es

    # ------------------------------------------------------------------
    # Build context snapshot
    # ------------------------------------------------------------------
    def build_context(
        self,
        device_id: str = "web_console",
        session_id: Optional[str] = None,
    ) -> dict:
        """
        Build a complete context snapshot for the current request.

        Returns a dict with:
          current_device, current_user, time, day_of_week, time_of_day,
          connected_devices, mood, os_info, environment
        """
        now = datetime.now()

        # Connected devices
        connected_devices = []
        if self._device_manager:
            try:
                devices = self._device_manager.get_all_devices()
                connected_devices = [
                    {
                        "device_id": d.device_id,
                        "device_type": d.device_type,
                        "status": d.status,
                        "capabilities": d.capabilities,
                        "battery": d.battery,
                    }
                    for d in devices
                ]
            except Exception as e:
                logger.warning("[CONTEXT] Failed to get devices: %s", e)

        # Current mood
        mood = None
        if self._emotional_service and session_id:
            try:
                state = self._emotional_service.get_state(session_id)
                mood = state.current_mood.value if state.current_mood else None
            except Exception as e:
                logger.debug("[CONTEXT] Failed to get mood: %s", e)

        return {
            "current_device": device_id,
            "current_user": BOSS_NAME,
            "assistant_name": ASSISTANT_NAME,
            "time": now.strftime("%H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "day_of_week": now.strftime("%A"),
            "time_of_day": _time_of_day_label(now.hour),
            "connected_devices": connected_devices,
            "device_count": len(connected_devices),
            "mood": mood,
            "os": platform.system(),
            "os_version": platform.version()[:40],
            "hostname": platform.node()[:30],
        }

    # ------------------------------------------------------------------
    # Format context for system prompt injection
    # ------------------------------------------------------------------
    def format_for_prompt(self, context: dict) -> str:
        """
        Format context as a compact block for injection into the system prompt.
        Kept short to minimize token usage.
        """
        lines = [
            "=== CURRENT CONTEXT ===",
            f"Time: {context['time']} | {context['day_of_week']} | {context['time_of_day']}",
            f"Date: {context['date']}",
            f"Device: {context['current_device']} | OS: {context['os']}",
        ]

        # Connected devices
        devices = context.get("connected_devices", [])
        if devices:
            device_strs = [
                f"{d['device_type']}({d['device_id']}, {d['status']})"
                for d in devices
            ]
            lines.append(f"Connected: {', '.join(device_strs)}")
        else:
            lines.append("Connected: web console only")

        # Mood
        mood = context.get("mood")
        if mood and mood != "neutral":
            lines.append(f"User mood: {mood}")

        return "\n".join(lines)


# Global context engine instance
context_engine = ContextEngine()
