# JARVIS Android Platform -- AI Brain + Remote Device Body

## Architecture Overview

```
   ANDROID DEVICE (Glasses / Phone)
          |
    JarvisSDK (Kotlin)
    - WebSocket connection
    - Voice capture/playback
    - Tool plugin execution
          |
   ====== INTERNET ======
          |
   JARVIS SERVER (existing FastAPI)
   - Brain: reasoning, memory, planning
   - Server tools: web search, file ops, PC control
   - Device tool routing: sends camera/GPS/etc calls to Android
```

The core principle: JARVIS never directly manipulates device hardware. It calls tools. Server-side tools execute on the server. Device-side tools are routed over WebSocket to the Android client, which executes them and returns results.

---

## PHASE 1: Server WebSocket API + Client Registry

### Step 1 -- Create client registry service
**New file:** `app/services/client_registry.py`

Tracks connected Android/remote clients:
```python
class ClientRegistry:
    _clients: dict  # client_id -> {"ws": WebSocket, "device_type": str, "capabilities": list, "connected_at": datetime}
    
    async def register(ws, client_id, device_type, capabilities)
    async def unregister(client_id)
    async def send_tool_call(client_id, tool_name, params) -> result  # sends tool call, waits for result
    def get_connected_clients() -> list
    def has_capability(capability) -> bool  # e.g. "camera", "gps", "bluetooth"
```

### Step 2 -- Create WebSocket endpoint
**File:** `app/main.py`

Add WebSocket endpoint at `/ws/client`:
```
ws://server:8000/ws/client?client_id=xxx&auth_token=yyy
```

Message protocol (JSON over WebSocket):
- `{"type": "register", "device_type": "glasses", "capabilities": ["camera", "gps", "bluetooth"]}`
- `{"type": "chat", "message": "text", "session_id": "xxx"}` -- client sends user message
- `{"type": "stream_chunk", "chunk": "text"}` -- server streams response
- `{"type": "tool_call", "id": "uuid", "tool": "camera.capture", "params": {}}` -- server requests tool execution
- `{"type": "tool_result", "id": "uuid", "result": {...}, "error": null}` -- client returns result
- `{"type": "audio", "data": "base64", "format": "pcm"}` -- voice audio
- `{"type": "tts", "text": "...", "audio": "base64_mp3"}` -- TTS response
- `{"type": "notification", "text": "...", "priority": "high"}` -- proactive notification

### Step 3 -- Device-aware tool executor
**File:** `app/services/tools/tool_executor.py`

Extend the tool executor to support remote tools:
- Add `REMOTE_TOOLS` registry: tool_name -> required_capability
- When a tool is called: check if it's local (SYSTEM_TOOLS) or remote (REMOTE_TOOLS)
- If remote: route to connected client via ClientRegistry, wait for result
- If no client has the capability: return "No connected device has this capability"

### Step 4 -- Define remote tool definitions
**New file:** `app/services/tools/remote_tools.py`

Define tools that execute on the Android device:
```python
REMOTE_TOOLS = {
    "camera_capture": {"capability": "camera", "description": "Take a photo", "params": []},
    "camera_record": {"capability": "camera", "description": "Record a short video", "params": ["duration_seconds"]},
    "gps_location": {"capability": "gps", "description": "Get current GPS coordinates", "params": []},
    "gps_navigate": {"capability": "gps", "description": "Open navigation to a location", "params": ["destination"]},
    "bluetooth_scan": {"capability": "bluetooth", "description": "Scan for nearby Bluetooth devices", "params": []},
    "bluetooth_connect": {"capability": "bluetooth", "description": "Connect to a Bluetooth device", "params": ["device_name"]},
    "phone_call": {"capability": "phone", "description": "Make a phone call", "params": ["number"]},
    "sms_send": {"capability": "phone", "description": "Send an SMS", "params": ["number", "message"]},
    "notification_read": {"capability": "notifications", "description": "Read device notifications", "params": []},
    "notification_send": {"capability": "notifications", "description": "Push a notification to device", "params": ["title", "text"]},
    "sensor_data": {"capability": "sensors", "description": "Get device sensor data (accelerometer, gyroscope)", "params": ["sensor_type"]},
    "storage_list": {"capability": "storage", "description": "List files on device", "params": ["path"]},
    "app_launch": {"capability": "apps", "description": "Launch an app on device", "params": ["package_name"]},
    "music_play": {"capability": "media", "description": "Play music on device", "params": ["query"]},
    "contacts_search": {"capability": "contacts", "description": "Search contacts", "params": ["name"]},
}
```

Register these in SYSTEM_TOOLS so the LLM can call them. When executed, they route to the connected Android client.

### Step 5 -- Auth token for client connections
**File:** `config.py` + `.env`

Add `JARVIS_AUTH_TOKEN` to .env -- shared secret for authenticating client connections. Only clients presenting this token can connect and execute tools.

---

## PHASE 2: Android SDK (Kotlin)

### Step 6 -- Create Android SDK project structure
**New directory:** `android-sdk/`

```
android-sdk/
  jarvis-sdk/
    src/main/java/ai/jarvis/sdk/
      JarvisClient.kt          -- Main entry point
      JarvisConfig.kt          -- Configuration (server URL, auth token, client ID)
      ws/
        JarvisWebSocket.kt     -- WebSocket connection manager
        MessageProtocol.kt     -- JSON message types
        ReconnectHandler.kt    -- Auto-reconnect with exponential backoff
      voice/
        VoiceCapture.kt        -- Microphone capture (AudioRecord)
        VoicePlayback.kt       -- Audio playback (AudioTrack / ExoPlayer)
        VoiceActivityDetector.kt -- Detect when user starts/stops speaking
      tools/
        ToolPlugin.kt          -- Interface for tool implementations
        ToolRegistry.kt        -- Registry of available device tools
        ToolExecutor.kt        -- Executes tool calls from server
      audio/
        AudioCodec.kt          -- PCM/Opus encoding/decoding
    build.gradle.kts
```

### Step 7 -- Core SDK: JarvisClient.kt
**New file:** `android-sdk/jarvis-sdk/src/main/java/ai/jarvis/sdk/JarvisClient.kt`

Main API that the Android app uses:
```kotlin
class JarvisClient(config: JarvisConfig) {
    fun connect()                                    // Connect to JARVIS server
    fun disconnect()                                 // Disconnect
    fun sendMessage(text: String)                    // Send text message
    fun sendAudio(audioData: ByteArray)              // Send voice audio
    fun registerTool(plugin: ToolPlugin)             // Register a device capability
    fun setListener(listener: JarvisListener)        // Set response listener
    
    interface JarvisListener {
        fun onStreamChunk(text: String)              // Real-time text streaming
        fun onStreamComplete(fullText: String)       // Full response received
        fun onAudioResponse(audioBytes: ByteArray)   // TTS audio received
        fun onToolCall(id: String, tool: String, params: Map<String, Any>)  // Tool execution requested
        fun onNotification(text: String, priority: String)  // Proactive notification
        fun onConnected()                            // Connection established
        fun onDisconnected(reason: String)           // Connection lost
        fun onError(error: Throwable)                // Error occurred
    }
}
```

### Step 8 -- WebSocket connection manager
**New file:** `android-sdk/jarvis-sdk/src/main/java/ai/jarvis/sdk/ws/JarvisWebSocket.kt`

OkHttp-based WebSocket client:
- Connects to `ws://server:8000/ws/client`
- Sends registration message with device capabilities
- Parses incoming JSON messages
- Routes tool calls to ToolExecutor
- Routes stream chunks to listener
- Auto-reconnect with exponential backoff (1s, 2s, 4s, 8s, max 30s)
- Heartbeat ping every 30s to detect stale connections

### Step 9 -- Tool plugin interface + registry
**New file:** `android-sdk/jarvis-sdk/src/main/java/ai/jarvis/sdk/tools/ToolPlugin.kt`

```kotlin
interface ToolPlugin {
    val name: String              // e.g. "camera_capture"
    val capability: String        // e.g. "camera"
    fun execute(params: Map<String, Any>): ToolResult
    fun isAvailable(): Boolean    // e.g. check if camera permission granted
}

data class ToolResult(val success: Boolean, val data: Any? = null, val error: String? = null)
```

### Step 10 -- Built-in tool plugins
**New directory:** `android-sdk/jarvis-sdk/src/main/java/ai/jarvis/sdk/tools/plugins/`

Implement core tool plugins:
- `CameraPlugin.kt` -- camera_capture, camera_record (uses CameraX)
- `GpsPlugin.kt` -- gps_location, gps_navigate (uses FusedLocationProvider)
- `BluetoothPlugin.kt` -- bluetooth_scan, bluetooth_connect (uses BluetoothAdapter)
- `PhonePlugin.kt` -- phone_call, sms_send (uses Intent)
- `NotificationPlugin.kt` -- notification_read, notification_send (uses NotificationManager)
- `SensorPlugin.kt` -- sensor_data (uses SensorManager)
- `ContactsPlugin.kt` -- contacts_search (uses ContentResolver)
- `MediaPlugin.kt` -- music_play (uses MediaStore + Intent)
- `AppPlugin.kt` -- app_launch (uses PackageManager + Intent)

Each plugin handles Android permissions gracefully. If permission not granted, `isAvailable()` returns false and the server knows the capability is unavailable.

---

## PHASE 3: Android Demo App

### Step 11 -- Create demo Android app
**New directory:** `android-app/`

Minimal app that uses the SDK:
- `MainActivity.kt` -- Connect/disconnect button, text input, chat display
- `VoiceActivity.kt` -- Voice interaction screen (hold-to-talk or always-listening)
- `JarvisApplication.kt` -- Initialize SDK, register tool plugins
- Registers all built-in tool plugins on startup
- Shows streaming text responses in a chat UI
- Plays TTS audio through speaker
- Displays notifications from JARVIS

### Step 12 -- Embed code snippet (for third-party apps)
**New file:** `android-sdk/EMBED.md` (only if you want documentation)

The embed snippet that any Android app can use to connect:
```kotlin
// 1. Add dependency
implementation("ai.jarvis:sdk:1.0.0")

// 2. Initialize
val jarvis = JarvisClient(JarvisConfig(
    serverUrl = "ws://YOUR_SERVER_IP:8000/ws/client",
    authToken = "YOUR_AUTH_TOKEN",
    deviceId = "my-glasses-001"
))

// 3. Register tools
jarvis.registerTool(CameraPlugin(context))
jarvis.registerTool(GpsPlugin(context))

// 4. Connect
jarvis.setListener(object : JarvisClient.JarvisListener {
    override fun onStreamChunk(text: String) { /* update UI */ }
    override fun onToolCall(id: String, tool: String, params: Map<String, Any>) {
        // SDK handles this automatically via registered plugins
    }
})
jarvis.connect()

// 5. Send message
jarvis.sendMessage("Take a picture and send it to Mom")
```

---

## PHASE 4: Voice Pipeline (Bidirectional)

### Step 13 -- Server-side voice endpoint
**File:** `app/main.py`

The existing `/api/transcribe` endpoint handles STT (speech-to-text) via Groq Whisper.
Add a streaming voice endpoint:
- `POST /voice/stream` -- accepts raw PCM audio, returns streaming text + TTS audio
- This is for the Android client to send voice and get voice back

### Step 14 -- SDK voice capture + playback
**Files:** `android-sdk/.../voice/VoiceCapture.kt`, `VoicePlayback.kt`

- VoiceCapture: uses Android AudioRecord to capture PCM at 16kHz mono
- VoicePlayback: uses ExoPlayer or AudioTrack to play TTS audio
- VoiceActivityDetector: simple energy-based VAD to detect speech start/stop
- Flow: capture voice -> send to server -> receive streaming text + TTS -> play audio

---

## Execution Order

1. Phase 1 (Steps 1-5): Server WebSocket API + remote tool routing -- foundation
2. Phase 2 (Steps 6-10): Android SDK -- the embeddable code
3. Phase 3 (Steps 11-12): Demo app + embed snippet
4. Phase 4 (Steps 13-14): Voice pipeline

## New Dependencies (server)
- `websockets` (already in FastAPI ecosystem via uvicorn)

## New Dependencies (Android SDK)
- `com.squareup.okhttp3:okhttp` -- WebSocket client
- `com.google.code.gson:gson` -- JSON parsing
- `androidx.camera:camera-core` -- CameraX for camera plugin
- `com.google.android.gms:play-services-location` -- GPS
- `com.google.android.exoplayer:exoplayer` -- Audio playback

## New .env Variables
- `JARVIS_AUTH_TOKEN` -- shared secret for client authentication

## New Directories
- `app/services/tools/remote_tools.py` -- remote tool definitions
- `app/services/client_registry.py` -- connected client tracking
- `android-sdk/` -- Kotlin SDK project
- `android-app/` -- Demo Android app

## Security
- All client connections require auth token
- Only registered clients can execute tools
- Tool execution results are validated before being passed back to LLM
- No device data is stored on the server unless explicitly saved by a tool
- All communication is JSON over WebSocket (can be wrapped in TLS for production)