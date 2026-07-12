"""
Connectors package for J.A.R.V.I.S.
=====================================
Each connector simulates or manages a real device type:
  - mock_android.py: Simulates an Android phone for testing
  - mock_glasses.py: Simulates ESP32 smart glasses for testing

When real hardware is ready, replace mock_* with real WebSocket connectors
that speak the same DeviceSession protocol.
"""
