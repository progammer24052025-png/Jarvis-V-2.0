"""
Mock Android Connector for J.A.R.V.I.S.
=========================================
Simulates an Android phone connected to the platform.
Registers with DeviceManager and provides mock capabilities:
  - camera (take photos, face detection)
  - gps (location, geocoding)
  - contacts (search, add)
  - notifications (read, send)
  - sms (read, send)
  - phone (dial, hang up)
  - bluetooth (scan, connect)
  - battery (status reporting)

Usage:
    from app.connectors.mock_android import start_mock_android
    start_mock_android()   # registers the mock device

When a real Android app is built, it connects via WebSocket and
replaces this mock. The DeviceManager interface stays identical.
"""

import logging
import threading
import time
from datetime import datetime

from app.services.device_manager import DeviceSession, device_manager

logger = logging.getLogger("J.A.R.V.I.S")

MOCK_ANDROID_DEVICE_ID = "mock-android-001"

# Mock data that the simulated phone "returns"
_MOCK_CONTACTS = [
    {"name": "Mom", "phone": "+1-555-0100", "email": "mom@example.com"},
    {"name": "Dad", "phone": "+1-555-0101", "email": "dad@example.com"},
    {"name": "Alice", "phone": "+1-555-0102", "email": "alice@example.com"},
    {"name": "Bob", "phone": "+1-555-0103", "email": "bob@example.com"},
]

_MOCK_NOTIFICATIONS = [
    {"app": "Gmail", "title": "Meeting at 3pm", "body": "Don't forget the quarterly review.", "time": "14:30"},
    {"app": "WhatsApp", "title": "Alice", "body": "Are we still on for lunch?", "time": "14:15"},
]

_MOCK_GPS = {"lat": 28.6139, "lng": 77.2090, "address": "New Delhi, India", "accuracy_m": 15}

_MOCK_BATTERY = 78
_MOCK_NETWORK = "wifi"


# ---------------------------------------------------------------------------
# Mock tool handlers — these simulate what the real Android app would do
# ---------------------------------------------------------------------------
def _mock_take_photo(params: dict) -> str:
    """Simulate taking a photo with the phone camera."""
    return "[Mock Android] Photo captured (front camera). File: mock_photo_001.jpg"


def _mock_get_location(params: dict) -> str:
    """Simulate getting GPS location."""
    g = _MOCK_GPS
    return f"[Mock Android] Location: {g['address']} ({g['lat']}, {g['lng']}) accuracy: {g['accuracy_m']}m"


def _mock_search_contacts(params: dict) -> str:
    """Simulate searching contacts."""
    query = str(params.get("query", "")).lower() if params else ""
    results = [c for c in _MOCK_CONTACTS if query in c["name"].lower()]
    if not results:
        return "[Mock Android] No contacts found."
    lines = [f"  {c['name']}: {c['phone']}" for c in results]
    return "[Mock Android] Contacts:\n" + "\n".join(lines)


def _mock_read_notifications(params: dict) -> str:
    """Simulate reading notifications."""
    if not _MOCK_NOTIFICATIONS:
        return "[Mock Android] No notifications."
    lines = [f"  [{n['app']}] {n['title']}: {n['body']} ({n['time']})" for n in _MOCK_NOTIFICATIONS]
    return "[Mock Android] Notifications:\n" + "\n".join(lines)


def _mock_send_sms(params: dict) -> str:
    """Simulate sending an SMS."""
    to = params.get("to", "unknown") if params else "unknown"
    msg = params.get("message", "") if params else ""
    return f"[Mock Android] SMS sent to {to}: {msg[:50]}"


def _mock_dial(params: dict) -> str:
    """Simulate dialing a phone number."""
    number = params.get("number", "") if params else ""
    return f"[Mock Android] Dialing {number}..."


def _mock_battery_status(params: dict) -> str:
    """Simulate battery status report."""
    return f"[Mock Android] Battery: {_MOCK_BATTERY}% | Network: {_MOCK_NETWORK} | Charging: No"


def _mock_bluetooth_scan(params: dict) -> str:
    """Simulate Bluetooth device scan."""
    return ("[Mock Android] Bluetooth scan results:\n"
            "  JBL Flip 5 (speaker)\n"
            "  WH-1000XM4 (headphones)\n"
            "  Pixel Buds Pro (earbuds)")


# ---------------------------------------------------------------------------
# Mock capability -> tool mapping
# ---------------------------------------------------------------------------
MOCK_ANDROID_TOOLS = {
    "android_take_photo": {
        "func": _mock_take_photo,
        "description": "Take a photo using the Android phone camera",
        "params": [],
        "capability": "camera",
    },
    "android_get_location": {
        "func": _mock_get_location,
        "description": "Get current GPS location from the Android phone",
        "params": [],
        "capability": "gps",
    },
    "android_search_contacts": {
        "func": _mock_search_contacts,
        "description": "Search contacts on the Android phone",
        "params": ["query"],
        "capability": "contacts",
    },
    "android_read_notifications": {
        "func": _mock_read_notifications,
        "description": "Read notifications from the Android phone",
        "params": [],
        "capability": "notifications",
    },
    "android_send_sms": {
        "func": _mock_send_sms,
        "description": "Send an SMS message via the Android phone",
        "params": ["to", "message"],
        "capability": "sms",
    },
    "android_dial": {
        "func": _mock_dial,
        "description": "Dial a phone number on the Android phone",
        "params": ["number"],
        "capability": "phone",
    },
    "android_battery_status": {
        "func": _mock_battery_status,
        "description": "Get battery and network status of the Android phone",
        "params": [],
        "capability": "battery",
    },
    "android_bluetooth_scan": {
        "func": _mock_bluetooth_scan,
        "description": "Scan for nearby Bluetooth devices via the Android phone",
        "params": [],
        "capability": "bluetooth",
    },
}


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------
_heartbeat_thread: threading.Thread = None
_stop_event = threading.Event()


def start_mock_android() -> DeviceSession:
    """
    Register a mock Android device with the DeviceManager.
    Starts a background heartbeat thread to simulate a live device.
    Returns the DeviceSession.
    """
    global _heartbeat_thread, _stop_event

    capabilities = [
        "camera", "gps", "contacts", "notifications",
        "sms", "phone", "bluetooth", "battery",
    ]

    device = DeviceSession(
        device_id=MOCK_ANDROID_DEVICE_ID,
        device_type="android",
        capabilities=capabilities,
        permissions={},
        battery=_MOCK_BATTERY,
        network=_MOCK_NETWORK,
        status="online",
        last_seen=datetime.now(),
        metadata={
            "model": "Mock Pixel 7",
            "android_version": "14",
            "installed_apps": [
                "Instagram", "WhatsApp", "Spotify", "YouTube",
                "Gmail", "Maps", "Chrome", "Telegram", "Discord",
                "Netflix", "Twitter", "Reddit", "LinkedIn",
                "Amazon", "Flipkart", "Uber", "Zoom", "Slack",
                "Notion", "ChatGPT", "GitHub", "TikTok",
            ],
        },
    )

    device_manager.register(device)
    logger.info("[MOCK-ANDROID] Registered mock Android device: %s", MOCK_ANDROID_DEVICE_ID)

    # Start heartbeat thread
    _stop_event = threading.Event()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(MOCK_ANDROID_DEVICE_ID,),
        daemon=True,
        name="mock-android-heartbeat",
    )
    _heartbeat_thread.start()

    return device


def stop_mock_android() -> None:
    """Unregister the mock Android device and stop the heartbeat."""
    global _heartbeat_thread, _stop_event
    _stop_event.set()
    device_manager.unregister(MOCK_ANDROID_DEVICE_ID)
    logger.info("[MOCK-ANDROID] Unregistered mock Android device")
    if _heartbeat_thread and _heartbeat_thread.is_alive():
        _heartbeat_thread.join(timeout=3)


def _heartbeat_loop(device_id: str) -> None:
    """Send periodic heartbeats to simulate a live device."""
    while not _stop_event.is_set():
        _stop_event.wait(15)  # heartbeat every 15 seconds
        if _stop_event.is_set():
            break
        device_manager.heartbeat(device_id, battery=_MOCK_BATTERY, network=_MOCK_NETWORK)


def get_mock_android_tools() -> dict:
    """Return the mock Android tools dict for registration in ToolRegistry."""
    return dict(MOCK_ANDROID_TOOLS)
