"""
Device Manager for J.A.R.V.I.S.
=================================
Manages all connected devices as equal clients.
Each device (Android phone, ESP32 glasses, Windows PC, web console)
registers with its capabilities and can receive tool calls.

The Device Manager replaces the old "Client Registry" concept with
a proper session model that tracks battery, network, permissions,
and health for each device.

All devices speak the same protocol. JARVIS doesn't care whether
it's talking to Android or ESP32 — it just routes to the right device.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from app.services.event_bus import event_bus

logger = logging.getLogger("J.A.R.V.I.S")


# ---------------------------------------------------------------------------
# Device session — represents one connected device
# ---------------------------------------------------------------------------
@dataclass
class DeviceSession:
    """Represents a single connected device."""
    device_id: str
    device_type: str              # "windows", "android", "esp32", "web"
    capabilities: List[str] = field(default_factory=list)   # ["camera", "gps", "bluetooth"]
    permissions: Dict[str, str] = field(default_factory=dict)  # capability -> "granted"|"denied"|"pending"
    battery: Optional[int] = None   # 0-100 or None
    network: str = "unknown"        # "wifi", "cellular", "ethernet", "bluetooth"
    status: str = "online"          # "online", "offline", "busy"
    last_seen: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, str] = field(default_factory=dict)  # device-specific info
    websocket: object = None        # WebSocket connection (if applicable)

    def is_online(self) -> bool:
        return self.status == "online"

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def is_capable_and_available(self, capability: str) -> bool:
        """Check if device has the capability AND is online AND permission is granted.
        If no explicit permission is set for the capability, it's assumed available."""
        if not self.is_online():
            return False
        if capability not in self.capabilities:
            return False
        perm = self.permissions.get(capability)
        # None/empty = no restriction; "denied" = blocked; anything else = allowed
        return perm != "denied"

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "capabilities": self.capabilities,
            "permissions": self.permissions,
            "battery": self.battery,
            "network": self.network,
            "status": self.status,
            "last_seen": self.last_seen.isoformat(),
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Device Manager
# ---------------------------------------------------------------------------
class DeviceManager:
    """
    Central registry for all connected devices.
    Thread-safe. Emits events on connect/disconnect/heartbeat.
    """

    def __init__(self, heartbeat_timeout: int = 60):
        self._devices: Dict[str, DeviceSession] = {}
        self._lock = threading.RLock()
        self._heartbeat_timeout = heartbeat_timeout  # seconds before device marked offline

    # ------------------------------------------------------------------
    # Register / Unregister
    # ------------------------------------------------------------------
    def register(self, device: DeviceSession) -> None:
        """Register a new device or re-register an existing one."""
        with self._lock:
            self._devices[device.device_id] = device
        logger.info("[DEVICE-MGR] Registered: %s (type=%s, caps=%s)",
                     device.device_id, device.device_type, device.capabilities)
        event_bus.emit("device_connected", {
            "device_id": device.device_id,
            "device_type": device.device_type,
            "capabilities": device.capabilities,
        })

    def unregister(self, device_id: str) -> Optional[DeviceSession]:
        """Remove a device from the registry."""
        with self._lock:
            device = self._devices.pop(device_id, None)
        if device:
            logger.info("[DEVICE-MGR] Unregistered: %s", device_id)
            event_bus.emit("device_disconnected", {
                "device_id": device_id,
                "device_type": device.device_type,
            })
        return device

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------
    def heartbeat(self, device_id: str, battery: Optional[int] = None,
                  network: Optional[str] = None) -> bool:
        """
        Update a device's heartbeat. Returns True if device exists.
        Updates last_seen, battery, network.
        """
        with self._lock:
            device = self._devices.get(device_id)
            if not device:
                return False
            device.last_seen = datetime.now()
            device.status = "online"
            if battery is not None:
                device.battery = battery
            if network is not None:
                device.network = network
        logger.debug("[DEVICE-MGR] Heartbeat: %s (battery=%s, network=%s)",
                      device_id, battery, network)
        event_bus.emit("device_heartbeat", {
            "device_id": device_id,
            "battery": battery,
            "network": network,
        })
        return True

    def check_health(self) -> List[str]:
        """
        Check all devices for stale heartbeats.
        Mark devices as offline if they haven't sent a heartbeat within timeout.
        Returns list of device_ids that were marked offline.
        """
        offline_devices = []
        now = datetime.now()
        with self._lock:
            for device in self._devices.values():
                if device.status != "online":
                    continue
                elapsed = (now - device.last_seen).total_seconds()
                if elapsed > self._heartbeat_timeout:
                    device.status = "offline"
                    offline_devices.append(device.device_id)
                    logger.warning("[DEVICE-MGR] Device %s marked offline (no heartbeat for %.0fs)",
                                   device.device_id, elapsed)
                    event_bus.emit("device_disconnected", {
                        "device_id": device.device_id,
                        "reason": "heartbeat_timeout",
                        "elapsed_seconds": elapsed,
                    })
        return offline_devices

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def get_device(self, device_id: str) -> Optional[DeviceSession]:
        with self._lock:
            return self._devices.get(device_id)

    def get_all_devices(self) -> List[DeviceSession]:
        with self._lock:
            return list(self._devices.values())

    def get_online_devices(self) -> List[DeviceSession]:
        with self._lock:
            return [d for d in self._devices.values() if d.status == "online"]

    def get_devices_by_capability(self, capability: str) -> List[DeviceSession]:
        """Return all devices that have a specific capability (regardless of status)."""
        with self._lock:
            return [d for d in self._devices.values() if capability in d.capabilities]

    def get_best_device(self, capability: str) -> Optional[DeviceSession]:
        """
        Find the best available device for a capability.
        Prefers: online > has permission > highest battery.
        Returns None if no device is suitable.
        """
        with self._lock:
            candidates = [
                d for d in self._devices.values()
                if d.is_capable_and_available(capability)
            ]
        if not candidates:
            return None

        # Sort by battery (highest first), then by last_seen (most recent first)
        candidates.sort(
            key=lambda d: (
                d.battery if d.battery is not None else 100,  # None battery = assume full
                d.last_seen.timestamp(),
            ),
            reverse=True,
        )
        return candidates[0]

    def get_device_count(self) -> int:
        with self._lock:
            return len(self._devices)

    def to_api_list(self) -> list:
        """Serialize all devices for the /api/devices endpoint."""
        with self._lock:
            return [d.to_dict() for d in self._devices.values()]

    # ------------------------------------------------------------------
    # Generate device ID
    # ------------------------------------------------------------------
    @staticmethod
    def generate_device_id(prefix: str = "device") -> str:
        """Generate a unique device ID."""
        return f"{prefix}-{uuid.uuid4().hex[:8]}"


# Global device manager instance
device_manager = DeviceManager()
