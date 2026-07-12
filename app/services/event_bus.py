"""
Event Bus for J.A.R.V.I.S.
============================
Central nervous system for inter-module communication.
All modules emit and subscribe to events instead of calling each other directly.

Benefits:
  - Easier debugging (all events logged in one place)
  - Audit trail for actions
  - Plugin system foundation
  - Decoupled architecture (modules don't import each other)

Event types:
  tool_started, tool_completed, tool_failed, tool_cached
  device_connected, device_disconnected, device_heartbeat
  user_message, assistant_response, mood_detected
  action_queued, action_started, action_completed, action_failed
  workflow_started, workflow_completed, workflow_failed
  system_info, error
"""

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger("J.A.R.V.I.S")

# Maximum events kept in the ring buffer for /api/events debugging
MAX_EVENT_HISTORY = 200


class EventBus:
    """
    Thread-safe event bus with sync and async handler support.
    Maintains a ring buffer of recent events for debugging.
    """

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self._event_history: Deque[dict] = deque(maxlen=MAX_EVENT_HISTORY)
        self._event_count = 0

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------
    def on(self, event_type: str, handler: Callable) -> None:
        """Register a handler for an event type."""
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            if handler not in self._handlers[event_type]:
                self._handlers[event_type].append(handler)
                logger.debug("[EVENT-BUS] Subscribed: %s -> %s", event_type, handler.__name__)

    def off(self, event_type: str, handler: Callable) -> None:
        """Unregister a handler for an event type."""
        with self._lock:
            if event_type in self._handlers:
                try:
                    self._handlers[event_type].remove(handler)
                    logger.debug("[EVENT-BUS] Unsubscribed: %s -> %s", event_type, handler.__name__)
                except ValueError:
                    pass

    # ------------------------------------------------------------------
    # Emit (sync — fire and forget for sync handlers)
    # ------------------------------------------------------------------
    def emit(self, event_type: str, data: Optional[dict] = None) -> None:
        """
        Emit an event. Calls all registered sync handlers immediately.
        Async handlers are scheduled on the current event loop if available.
        Records the event in history for debugging.
        """
        data = data or {}
        timestamp = time.time()
        self._event_count += 1

        # Record in history
        event_record = {
            "id": self._event_count,
            "type": event_type,
            "data": data,
            "timestamp": timestamp,
        }
        self._event_history.append(event_record)

        # Get handlers snapshot
        with self._lock:
            handlers = list(self._handlers.get(event_type, []))

        if not handlers:
            logger.debug("[EVENT-BUS] Event '%s' emitted (no handlers)", event_type)
            return

        # Call each handler
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    # Try to schedule on running loop
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(handler(event_type, data))
                    except RuntimeError:
                        # No running loop — skip async handler
                        logger.debug("[EVENT-BUS] Async handler %s skipped (no loop)", handler.__name__)
                else:
                    handler(event_type, data)
            except Exception as e:
                logger.error("[EVENT-BUS] Handler %s error on '%s': %s",
                             handler.__name__, event_type, e)

        logger.debug("[EVENT-BUS] Event '%s' -> %d handler(s)", event_type, len(handlers))

    # ------------------------------------------------------------------
    # Async emit (await all async handlers)
    # ------------------------------------------------------------------
    async def async_emit(self, event_type: str, data: Optional[dict] = None) -> None:
        """
        Emit an event and await all async handlers.
        Sync handlers are called immediately (not awaited).
        """
        data = data or {}
        timestamp = time.time()
        self._event_count += 1

        event_record = {
            "id": self._event_count,
            "type": event_type,
            "data": data,
            "timestamp": timestamp,
        }
        self._event_history.append(event_record)

        with self._lock:
            handlers = list(self._handlers.get(event_type, []))

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event_type, data)
                else:
                    handler(event_type, data)
            except Exception as e:
                logger.error("[EVENT-BUS] Async handler %s error on '%s': %s",
                             handler.__name__, event_type, e)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def recent_events(self, limit: int = 50, event_type: Optional[str] = None) -> list:
        """
        Return recent events from the ring buffer.
        Optionally filter by event_type.
        """
        events = list(self._event_history)
        if event_type:
            events = [e for e in events if e["type"] == event_type]
        return events[-limit:]

    def stats(self) -> dict:
        """Return event bus statistics."""
        with self._lock:
            handler_count = sum(len(h) for h in self._handlers.values())
            event_types = list(self._handlers.keys())
        return {
            "total_events": self._event_count,
            "history_size": len(self._event_history),
            "registered_handlers": handler_count,
            "event_types": event_types,
        }

    def clear_history(self) -> None:
        """Clear the event history buffer."""
        self._event_history.clear()
        logger.info("[EVENT-BUS] History cleared")


# Global event bus instance
event_bus = EventBus()
