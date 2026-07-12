"""
Mock ESP32 Smart Glasses Connector for J.A.R.V.I.S.
=====================================================
Simulates ESP32-based smart glasses connected to the platform.
Registers with DeviceManager and provides mock capabilities:
  - camera (wide-angle, face detection)
  - microphone (voice capture)
  - speaker (audio output)
  - display (small OLED heads-up display)
  - imu (accelerometer, gyroscope, head tracking)
  - bone_conduction (audio via bone conduction)

Usage:
    from app.connectors.mock_glasses import start_mock_glasses
    start_mock_glasses()   # registers the mock device

When real ESP32 glasses are built, they connect via WebSocket and
replace this mock. The DeviceManager interface stays identical.
"""

import logging
import threading
import time
from datetime import datetime

from app.services.device_manager import DeviceSession, device_manager

logger = logging.getLogger("J.A.R.V.I.S")

MOCK_GLASSES_DEVICE_ID = "mock-glasses-001"

# Mock sensor data
_MOCK_IMU = {"pitch": 2.5, "roll": -0.3, "yaw": 45.0, "steps": 342}
_MOCK_BATTERY = 92
_MOCK_NETWORK = "bluetooth"


# ---------------------------------------------------------------------------
# Mock tool handlers
# ---------------------------------------------------------------------------
def _mock_glasses_capture_photo(params: dict) -> str:
    """Simulate capturing a photo from the glasses camera."""
    return "[Mock Glasses] Photo captured (wide-angle). File: glasses_photo_001.jpg"


def _mock_glasses_start_recording(params: dict) -> str:
    """Simulate starting audio/video recording."""
    duration = params.get("duration", "30") if params else "30"
    return f"[Mock Glasses] Recording started (max {duration}s). Audio + video."


def _mock_glasses_stop_recording(params: dict) -> str:
    """Simulate stopping recording."""
    return "[Mock Glasses] Recording stopped. File: glasses_recording_001.mp4"


def _mock_glasses_display_text(params: dict) -> str:
    """Simulate showing text on the OLED heads-up display."""
    text = params.get("text", "") if params else ""
    return f"[Mock Glasses] Displaying on HUD: '{text[:60]}'"


def _mock_glasses_read_text(params: dict) -> str:
    """Simulate OCR from the glasses camera (reading text in real world)."""
    return ("[Mock Glasses] OCR result from camera:\n"
            "  'Welcome to the conference room'\n"
            "  'Meeting starts at 3:00 PM'")


def _mock_glasses_face_detect(params: dict) -> str:
    """Simulate face detection from glasses camera."""
    return ("[Mock Glasses] Face detection:\n"
            "  Face 1: Unknown male, facing camera, ~1.5m away\n"
            "  Face 2: Unknown female, profile view, ~2m away")


def _mock_glasses_imu(params: dict) -> str:
    """Simulate IMU (head orientation) data."""
    imu = _MOCK_IMU
    return (f"[Mock Glasses] Head orientation: pitch={imu['pitch']}°, "
            f"roll={imu['roll']}°, yaw={imu['yaw']}° | Steps: {imu['steps']}")


def _mock_glasses_battery(params: dict) -> str:
    """Simulate battery status."""
    return f"[Mock Glasses] Battery: {_MOCK_BATTERY}% | Network: {_MOCK_NETWORK} | Status: Discharging"


def _mock_glasses_speak(params: dict) -> str:
    """Simulate text-to-speech through glasses speaker."""
    text = params.get("text", "") if params else ""
    return f"[Mock Glasses] Speaking via bone conduction: '{text[:60]}'"


# ---------------------------------------------------------------------------
# Mock capability -> tool mapping
# ---------------------------------------------------------------------------
MOCK_GLASSES_TOOLS = {
    "glasses_capture_photo": {
        "func": _mock_glasses_capture_photo,
        "description": "Capture a photo using the smart glasses camera",
        "params": [],
        "capability": "camera",
    },
    "glasses_start_recording": {
        "func": _mock_glasses_start_recording,
        "description": "Start audio/video recording on the smart glasses",
        "params": ["duration"],
        "capability": "camera",
    },
    "glasses_stop_recording": {
        "func": _mock_glasses_stop_recording,
        "description": "Stop recording on the smart glasses",
        "params": [],
        "capability": "camera",
    },
    "glasses_display_text": {
        "func": _mock_glasses_display_text,
        "description": "Display text on the smart glasses HUD",
        "params": ["text"],
        "capability": "display",
    },
    "glasses_read_text": {
        "func": _mock_glasses_read_text,
        "description": "Use the glasses camera to read text (OCR) in the real world",
        "params": [],
        "capability": "camera",
    },
    "glasses_face_detect": {
        "func": _mock_glasses_face_detect,
        "description": "Detect faces visible to the smart glasses camera",
        "params": [],
        "capability": "camera",
    },
    "glasses_imu": {
        "func": _mock_glasses_imu,
        "description": "Get head orientation and step count from the glasses IMU",
        "params": [],
        "capability": "imu",
    },
    "glasses_battery": {
        "func": _mock_glasses_battery,
        "description": "Get battery status of the smart glasses",
        "params": [],
        "capability": "battery",
    },
    "glasses_speak": {
        "func": _mock_glasses_speak,
        "description": "Speak text through the smart glasses bone conduction speaker",
        "params": ["text"],
        "capability": "speaker",
    },
}


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------
_heartbeat_thread: threading.Thread = None
_stop_event = threading.Event()


def start_mock_glasses() -> DeviceSession:
    """
    Register a mock ESP32 glasses device with the DeviceManager.
    Starts a background heartbeat thread to simulate a live device.
    Returns the DeviceSession.
    """
    global _heartbeat_thread, _stop_event

    capabilities = [
        "camera", "microphone", "speaker", "display",
        "imu", "bone_conduction", "battery",
    ]

    device = DeviceSession(
        device_id=MOCK_GLASSES_DEVICE_ID,
        device_type="esp32",
        capabilities=capabilities,
        permissions={},
        battery=_MOCK_BATTERY,
        network=_MOCK_NETWORK,
        status="online",
        last_seen=datetime.now(),
        metadata={"model": "Mock ESP32 Glasses v1", "firmware": "0.1.0-mock"},
    )

    device_manager.register(device)
    logger.info("[MOCK-GLASSES] Registered mock ESP32 glasses: %s", MOCK_GLASSES_DEVICE_ID)

    # Start heartbeat thread (glasses send heartbeat less frequently to save power)
    _stop_event = threading.Event()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(MOCK_GLASSES_DEVICE_ID,),
        daemon=True,
        name="mock-glasses-heartbeat",
    )
    _heartbeat_thread.start()

    return device


def stop_mock_glasses() -> None:
    """Unregister the mock glasses device and stop the heartbeat."""
    global _heartbeat_thread, _stop_event
    _stop_event.set()
    device_manager.unregister(MOCK_GLASSES_DEVICE_ID)
    logger.info("[MOCK-GLASSES] Unregistered mock ESP32 glasses")
    if _heartbeat_thread and _heartbeat_thread.is_alive():
        _heartbeat_thread.join(timeout=3)


def _heartbeat_loop(device_id: str) -> None:
    """Send periodic heartbeats to simulate a live device."""
    while not _stop_event.is_set():
        _stop_event.wait(20)  # heartbeat every 20 seconds (lower frequency = save power)
        if _stop_event.is_set():
            break
        device_manager.heartbeat(device_id, battery=_MOCK_BATTERY, network=_MOCK_NETWORK)


def get_mock_glasses_tools() -> dict:
    """Return the mock glasses tools dict for registration in ToolRegistry."""
    return dict(MOCK_GLASSES_TOOLS)
