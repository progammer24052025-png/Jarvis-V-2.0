from pathlib import Path
from fastapi import FastAPI, HTTPException, Request as FastAPIRequest, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from contextlib import asynccontextmanager
import uvicorn
import logging
import json
import time
import re
import base64
import asyncio
import os
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import edge_tts
from pydantic import BaseModel
from app.models import ChatRequest, ChatResponse, TTSRequest, SettingsUpdate
import config as config_module
from app.services.tools.tool_executor import process_text_for_actions
from langchain_core.messages import SystemMessage, HumanMessage

RATE_LIMIT_MESSAGE = (
    "You've reached your daily API limit for this assistant. "
    "Your credits will reset in a few hours, or you can upgrade your plan for more. "
    "Please try again later."
)

def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in str(exc) or "rate limit" in msg or "tokens per day" in msg

from app.services.vector_store import VectorStoreService
from app.services.groq_service import GroqService, AllGroqApisFailedError
from app.services.realtime_service import RealtimeGroqService
from app.services.chat_service import ChatService
from app.services.brain_service import BrainService
from config import (
    GROQ_API_KEYS, GROQ_MODEL, TAVILY_API_KEY,
    EMBEDDING_MODEL, CHUNK_SIZE, CHUNK_OVERLAP, MAX_CHAT_HISTORY_TURNS,
    ASSISTANT_NAME, CORS_ORIGINS, APP_STATE_DIR,
)

# ── Simple in-memory rate limiter (no external dependency) ──
_rate_limit_store: dict = defaultdict(list)  # ip -> [timestamps]
RATE_LIMIT_MAX = 30     # max requests per window
RATE_LIMIT_WINDOW = 60  # window in seconds

def _check_rate_limit(request: Request) -> bool:
    """Return True if the request should be rate-limited (denied).
    Keys on IP + User-Agent to avoid localhost collision (Step 10 fix)."""
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")
    rate_key = f"{client_ip}:{user_agent}"
    now = time.time()
    timestamps = _rate_limit_store[rate_key]
    # Prune old entries
    _rate_limit_store[rate_key] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[rate_key]) >= RATE_LIMIT_MAX:
        return True
    _rate_limit_store[rate_key].append(now)
    return False

# ── Logging setup (supports LOG_FORMAT=json for structured logs) ──
_log_format = os.environ.get("LOG_FORMAT", "text").strip().lower()
if _log_format == "json":
    from app.utils.logging_config import setup_logging
    setup_logging(log_format="json")
else:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
logger = logging.getLogger("J.A.R.V.I.S")

vector_store_service: VectorStoreService = None
groq_service: GroqService = None
realtime_service: RealtimeGroqService = None
brain_service: BrainService = None
chat_service: ChatService = None

# Per-session uploaded file context — injected into the next chat message
# Key: session_id, Value: {"filename": str, "content": str (truncated), "file_type": str}
uploaded_file_context: dict = {}

# Persistent file storage - tracks all uploaded files with metadata
# Key: filename, Value: {"filename": str, "original_name": str, "content": str, "file_type": str, "words": int, "uploaded_at": str}
uploaded_files_db: dict = {}

# Reminder system storage
# Key: reminder_id, Value: {"id": str, "message": str, "remind_at": datetime, "repeat": str, "sound": str, "speak": bool, "active": bool}
reminders_db: dict = {}
_reminder_task: asyncio.Task = None
_health_task: asyncio.Task = None
_shutdown_check_task: asyncio.Task = None
_jarvis_shutting_down = False  # Set to True when exit_jarvis is triggered

# Webhook storage for AI completion notifications
# Key: webhook_id, Value: {"id": str, "url": str, "events": list, "active": bool}
webhooks_db: dict = {}

# Email configuration
email_config: dict = {"smtp_host": "", "smtp_port": 587, "username": "", "password": "", "from_email": ""}

# Notion configuration
notion_config: dict = {"api_key": "", "database_id": ""}

# Google Calendar configuration
calendar_config: dict = {"credentials_json": "", "token_json": ""}

# Slack configuration
slack_config: dict = {"bot_token": "", "signing_secret": "", "app_token": ""}


# ── App State Persistence ──
# Save/load in-memory state dicts to disk so they survive restarts.

def _save_app_state():
    """Persist all in-memory state dicts to disk as JSON."""
    state = {
        "reminders": reminders_db,
        "webhooks": webhooks_db,
        "uploaded_files": uploaded_files_db,
        "integrations": {
            "email": email_config,
            "notion": notion_config,
            "calendar": calendar_config,
            "slack": slack_config,
        },
    }
    filepath = APP_STATE_DIR / "app_state.json"
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("[STATE] Failed to save app state: %s", e)


def _load_app_state():
    """Load persisted state dicts from disk into memory."""
    global reminders_db, webhooks_db, uploaded_files_db

    filepath = APP_STATE_DIR / "app_state.json"
    if not filepath.exists():
        logger.info("[STATE] No saved app state found, starting fresh")
        return

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            state = json.load(f)

        reminders_db = state.get("reminders", {})
        webhooks_db = state.get("webhooks", {})
        uploaded_files_db = state.get("uploaded_files", {})

        integrations = state.get("integrations", {})
        email_config.update(integrations.get("email", {}))
        notion_config.update(integrations.get("notion", {}))
        calendar_config.update(integrations.get("calendar", {}))
        slack_config.update(integrations.get("slack", {}))

        logger.info(
            "[STATE] Loaded app state: %d reminders, %d webhooks, %d uploaded files",
            len(reminders_db), len(webhooks_db), len(uploaded_files_db),
        )
    except Exception as e:
        logger.warning("[STATE] Failed to load app state: %s", e)


async def _check_shutdown_flag():
    """Background task that checks for the JARVIS shutdown flag file (set by exit_jarvis tool)."""
    import os
    global _jarvis_shutting_down
    flag_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".jarvis_shutdown")
    while True:
        try:
            await asyncio.sleep(2)
            if os.path.exists(flag_path):
                logger.info("[SHUTDOWN] Shutdown flag detected. Initiating graceful shutdown.")
                os.remove(flag_path)
                _jarvis_shutting_down = True
                # Give streams 2 seconds to deliver the goodbye message
                await asyncio.sleep(2)
                # Trigger uvicorn shutdown
                import signal
                os.kill(os.getpid(), signal.SIGTERM)
                break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("[SHUTDOWN] Flag check error: %s", e)


async def _run_reminder_checker():
    """Background task to check and trigger reminders."""
    while True:
        try:
            await asyncio.sleep(5)  # Check every 5 seconds
            now = datetime.now()
            
            for reminder_id, reminder in list(reminders_db.items()):
                if not reminder.get("active", True):
                    continue
                
                remind_dt = datetime.fromisoformat(reminder["remind_at"])
                if remind_dt <= now:
                    # Trigger reminder
                    logger.info("[REMINDER] Triggering: %s - %s", reminder_id, reminder["message"])
                    
                    # Trigger webhooks
                    await trigger_webhook("reminder_triggered", {
                        "reminder_id": reminder_id,
                        "message": reminder["message"],
                        "sound": reminder.get("sound", "default"),
                        "speak": reminder.get("speak", False),
                    })
                    
                    # Handle repeat
                    repeat = reminder.get("repeat", "none")
                    if repeat == "none":
                        reminder["active"] = False
                    elif repeat == "daily":
                        reminder["remind_at"] = (remind_dt + timedelta(days=1)).isoformat()
                    elif repeat == "weekly":
                        reminder["remind_at"] = (remind_dt + timedelta(weeks=1)).isoformat()
                    elif repeat == "monthly":
                        # Use manual month arithmetic to avoid drift (Step 12 fix)
                        _m = remind_dt.month + 1
                        _y = remind_dt.year + (1 if _m > 12 else 0)
                        _m = ((_m - 1) % 12) + 1
                        import calendar as _cal
                        _max_day = _cal.monthrange(_y, _m)[1]
                        _d = min(remind_dt.day, _max_day)
                        reminder["remind_at"] = remind_dt.replace(year=_y, month=_m, day=_d).isoformat()
                    _save_app_state()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("[REMINDER] Check error: %s", e)



def search_vector_store_for_files(query: str, top_k: int = 3) -> list:
    """Search the vector store for relevant file content."""
    if not vector_store_service or not vector_store_service.vector_store:
        return []
    
    try:
        retriever = vector_store_service.get_retriever(k=top_k)
        docs = retriever.invoke(query)
        results = []
        for doc in docs:
            source = doc.metadata.get("source", "")
            # Only return results from uploaded files (source starts with "upload_")
            if source.startswith("upload_"):
                results.append({
                    "source": source.replace("upload_", ""),
                    "content": doc.page_content,
                })
        return results
    except Exception as e:
        logger.warning("[VECTOR] Search failed: %s", e)
        return []

def _inject_file_context(session_id: str, user_message: str) -> str:
    """Inject uploaded file context and vector search results into user message."""
    file_ctx = uploaded_file_context.get(session_id) or uploaded_file_context.get("global")
    vector_file_results = search_vector_store_for_files(user_message, top_k=2)

    if not file_ctx and not vector_file_results:
        return user_message

    context_parts = []
    if file_ctx:
        context_parts.append(
            f"[The user previously uploaded a file: {file_ctx['filename']} ({file_ctx['file_type']}, {file_ctx['words']} words)]\n"
            f"--- FILE CONTENT ---\n{file_ctx['content']}\n--- END FILE CONTENT ---"
        )
    if vector_file_results:
        for result in vector_file_results:
            context_parts.append(
                f"[Relevant content from uploaded file: {result['source']}]\n"
                f"--- CONTENT ---\n{result['content'][:2000]}\n--- END CONTENT ---"
            )

    enhanced_msg = f"{chr(10).join(context_parts)}\n\nUser's question: {user_message}"
    logger.info("[UPLOAD-CONTEXT] Injected file context + vector search results into session %s", session_id[:12])
    return enhanced_msg

def print_title():
    """Print the J.A.R.V.I.S ASCII art title."""
    import sys
    fancy = """
   ╔══════════════════════════════════════════════════════════╗
   ║                                                          ║
   ║         ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗          ║
   ║         ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝          ║
   ║         ██║███████║██████╔╝██║   ██║██║███████╗          ║
   ║    ██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║          ║
   ║    ╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║          ║
   ║     ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝          ║
   ║                                                          ║
   ║          Just A Rather Very Intelligent System           ║
   ║                                                          ║
   ╚══════════════════════════════════════════════════════════╝

    """
    plain = """
  ==========================================================
  |                                                        |
  |           J.A.R.V.I.S  Starting Up...                  |
  |       Just A Rather Very Intelligent System            |
  |                                                        |
  ==========================================================
    """
    enc = (sys.stdout.encoding or "ascii").lower()
    if enc in ("utf-8", "utf8"):
        print(fancy)
    else:
        print(plain)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global vector_store_service, groq_service, realtime_service, brain_service, chat_service, _reminder_task, _shutdown_check_task

    print_title()
    logger.info("=" * 60)
    logger.info("J.A.R.V.I.S - Starting Up...")
    logger.info("=" * 60)
    logger.info("[CONFIG] Assistant name: %s", ASSISTANT_NAME)
    logger.info("[CONFIG] Groq model: %s", GROQ_MODEL)
    logger.info("[CONFIG] Groq API keys loaded: %d", len(GROQ_API_KEYS))
    logger.info("[CONFIG] CORS origins: %s", CORS_ORIGINS)
    logger.info("[CONFIG] Tavily API key: %s", "configured" if TAVILY_API_KEY else "NOT SET")
    logger.info("[CONFIG] Embedding model: %s", EMBEDDING_MODEL)
    logger.info("[CONFIG] Chunk size: %d | Overlap: %d | Max history turns: %d",
                CHUNK_SIZE, CHUNK_OVERLAP, MAX_CHAT_HISTORY_TURNS)

    # Load persisted app state (reminders, webhooks, integration configs)
    _load_app_state()

    try:
        logger.info("Initializing vector store service...")
        t0 = time.perf_counter()
        vector_store_service = VectorStoreService()
        vector_store_service.create_vector_store()
        logger.info("[TIMING] startup_vector_store: %.3fs", time.perf_counter() - t0)

        logger.info("Initializing Groq service (general queries)...")
        groq_service = GroqService(vector_store_service)
        logger.info("Groq service initialized successfully")

        logger.info("Initializing Realtime Groq service (with Tavily search)...")
        realtime_service = RealtimeGroqService(vector_store_service)
        logger.info("Realtime Groq service initialized successfully")

        logger.info("Initializing Brain service (Groq query classification)...")
        brain_service = BrainService()
        logger.info("Brain service initialized successfully")

        logger.info("Initializing chat service...")
        from app.services.context_engine import context_engine as _ctx_engine
        chat_service = ChatService(groq_service, realtime_service, brain_service, context_engine=_ctx_engine)
        logger.info("Chat service initialized successfully")

        # Start reminder Background task
        logger.info("Starting reminder monitoring service...")
        _reminder_task = asyncio.create_task(_run_reminder_checker())
        logger.info("Reminder service started")
        
        # Start health monitor background task
        logger.info("Starting PC health monitor...")
        from app.services.health_service import get_health_monitor
        health_monitor = get_health_monitor()
        health_monitor.start()
        logger.info("Health monitor started")
        
        # Start shutdown flag checker (for exit_jarvis tool)
        _shutdown_check_task = asyncio.create_task(_check_shutdown_flag())
        logger.info("Shutdown flag checker started")

        # --- Phase 0 Platform Modules ---
        logger.info("Initializing Phase 0 platform modules...")

        # Tool Registry: load all local tools into the universal schema
        from app.services.tools.tool_schema import tool_registry
        from app.services.tools.tool_executor import REQUIRES_CONFIRMATION
        tool_registry.load_from_system_tools(REQUIRES_CONFIRMATION)
        logger.info("[PLATFORM] Tool Registry: %d tools loaded", len(tool_registry.all_tools()))

        # App Inventory: scan registry + Start Menu for installed apps
        try:
            from app.services.tools.system_tools import _get_inventory
            _app_count = len(_get_inventory())
            logger.info("[PLATFORM] App Inventory: %d apps indexed", _app_count)
        except Exception as e:
            _app_count = 0
            logger.warning("[PLATFORM] App Inventory scan failed: %s", e)

        # Action Manager: wire up the executor
        from app.services.action_manager import action_manager
        from app.services.tools.tool_executor import execute_action
        action_manager.set_executor(execute_action)
        logger.info("[PLATFORM] Action Manager: executor wired")

        # Context Engine: wire up device manager and emotional service
        from app.services.context_engine import context_engine
        from app.services.device_manager import device_manager
        context_engine.set_device_manager(device_manager)
        try:
            from app.services.emotional_service import emotional_intelligence
            context_engine.set_emotional_service(emotional_intelligence)
        except Exception:
            pass  # emotional service is optional
        logger.info("[PLATFORM] Context Engine: wired")

        # Mock connectors: register simulated devices for testing
        import os as _os
        if _os.getenv("MOCK_DEVICES_ENABLED", "true").lower() in ("true", "1", "yes"):
            try:
                from app.connectors.mock_android import start_mock_android
                from app.connectors.mock_glasses import start_mock_glasses
                start_mock_android()
                start_mock_glasses()
                logger.info("[PLATFORM] Mock devices: Android + Glasses registered")
            except Exception as e:
                logger.warning("[PLATFORM] Mock devices failed to start: %s", e)

        # Custom Action Engine: load persisted user-created tools
        if _os.getenv("CUSTOM_ACTIONS_ENABLED", "true").lower() in ("true", "1", "yes"):
            try:
                from app.services.tools.custom_action_manager import custom_action_manager
                from config import CURRENT_USER_ID
                _loaded = custom_action_manager.load_actions(CURRENT_USER_ID)
                _custom_count = tool_registry.load_custom_tools(CURRENT_USER_ID)
                logger.info("[PLATFORM] Custom Action Engine: %d actions loaded for user '%s'", _loaded, CURRENT_USER_ID)
            except Exception as e:
                logger.warning("[PLATFORM] Custom Action Engine failed to start: %s", e)

        logger.info("=" * 60)
        logger.info("Service Status:")
        logger.info("  - Vector Store: Ready")
        logger.info("  - Groq AI (General): Ready")
        logger.info("  - Groq AI (Realtime): Ready")
        logger.info("  - Brain (Groq): Ready")
        logger.info("  - Chat Service: Ready")
        logger.info("  - Reminder Service: Ready")
        logger.info("  - Health Monitor: Ready")
        logger.info("  - Tool Registry: %d tools", len(tool_registry.all_tools()))
        logger.info("  - Device Manager: %d devices", device_manager.get_device_count())
        logger.info("  - Action Manager: Ready")
        logger.info("  - Context Engine: Ready")
        logger.info("  - Event Bus: Active")
        logger.info("  - Tool Cache: Active")
        logger.info("  - Custom Action Engine: %d tools", _custom_count if 'CUSTOM_ACTIONS_ENABLED' in dir() and _os.getenv('CUSTOM_ACTIONS_ENABLED', 'true').lower() in ('true', '1', 'yes') else 0)
        logger.info("  - App Inventory: %d apps", _app_count)
        logger.info("=" * 60)
        logger.info("J.A.R.V.I.S is online and ready!")
        logger.info("API: http://localhost:8000")
        logger.info("Frontend: http://localhost:8000/app/ (open in browser)")
        logger.info("Platform Status: http://localhost:8000/api/platform/status")
        logger.info("=" * 60)

        yield

        # Cancel reminder task on shutdown
        if _reminder_task:
            _reminder_task.cancel()
            try:
                await _reminder_task
            except asyncio.CancelledError:
                pass

        # Stop health monitor
        from app.services.health_service import get_health_monitor
        get_health_monitor().stop()

        # Stop shutdown checker
        if _shutdown_check_task:
            _shutdown_check_task.cancel()
            try:
                await _shutdown_check_task
            except asyncio.CancelledError:
                pass

        # Stop mock connectors
        try:
            from app.connectors.mock_android import stop_mock_android
            from app.connectors.mock_glasses import stop_mock_glasses
            stop_mock_android()
            stop_mock_glasses()
            logger.info("Mock devices stopped")
        except Exception:
            pass

        logger.info("\nShutting down J.A.R.V.I.S...")
        _tts_pool.shutdown(wait = True)
        if chat_service:
            for session_id in list(chat_service.sessions.keys()):
                chat_service.save_chat_session(session_id)
            logger.info("All sessions saved. Goodbye!")

    except Exception as e:
        logger.error(f"Fatal error during startup: {e}", exc_info=True)
        raise

app = FastAPI(
    title="J.A.R.V.I.S API",
    description="Just A Rather Very Intelligent System",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

app.add_middleware(GZipMiddleware, minimum_size=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - t0
        path = request.url.path
        logger.info("[REQUEST] %s %s -> %s (%.3fs)", request.method, path, response.status_code, elapsed)
        return response

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiter for chat endpoints."""
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "POST" and path.startswith("/chat"):
            if _check_rate_limit(request):
                from starlette.responses import JSONResponse
                logger.warning("[RATE-LIMIT] Blocked %s from %s", path, request.client.host if request.client else "?")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please slow down (max 30/min)."}
                )
        return await call_next(request)

app.add_middleware(TimingMiddleware)
app.add_middleware(RateLimitMiddleware)

@app.get("/api")
async def api_info():
    return {
        "message": "J.A.R.V.I.S API",
        "endpoints": {
            "/chat": "General chat (non-streaming)",
            "/chat/stream": "General chat (streaming chunks)",
            "/chat/realtime": "Realtime chat (non-streaming)",
            "/chat/realtime/stream": "Realtime chat (streaming chunks)",
            "/chat/jarvis/stream": "Jarvis unified route (brain classifies, streams)",
            "/chat/history/{session_id}": "Get chat history",
            "/health": "System health check",
            "/tts": "Text-to-speech (POST text, returns streamed MP3)"
        }
    }

@app.get("/health")
async def health():
    try:
        return {
            "status": "healthy",
            "vector_store": vector_store_service is not None,
            "groq_service": groq_service is not None,
            "realtime_service": realtime_service is not None,
            "brain_service": brain_service is not None,
            "chat_service": chat_service is not None
        }
    except Exception as e:
        logger.warning("[API /health] Error: %s", e)
        return {"status": "degraded", "error": str(e)}

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    if not chat_service:
        raise HTTPException(status_code=503, detail="Chat service not initialized")

    logger.info("[API /chat] Incoming | session_id=%s | message_len=%d | message=%.100s",
                request.session_id or "new", len(request.message), request.message)
    try:
        session_id = chat_service.get_or_create_session(request.session_id)
        response_text = chat_service.process_message(session_id, request.message)
        chat_service.save_chat_session(session_id)
        logger.info("[API /chat] Done | session_id=%s | response_len=%d", session_id[:12], len(response_text))
        return ChatResponse(response=response_text, session_id=session_id)
    except ValueError as e:
        logger.warning("[API /chat] Invalid session_id: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except AllGroqApisFailedError as e:
        logger.error("[API /chat] All Groq APIs failed: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        if _is_rate_limit_error(e):
            logger.warning("[API /chat] Rate limit hit: %s", e)
            raise HTTPException(status_code=429, detail=RATE_LIMIT_MESSAGE)
        logger.error("[API /chat] Error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing chat: {str(e)}")

_SPLIT_RE = re.compile(r"(?<=[.!?,;:])\s+")
_MIN_WORDS_FIRST = 2
_MIN_WORDS = 3
_MERGE_IF_WORDS = 2

def _split_sentences(buf: str):
    parts = _SPLIT_RE.split(buf)
    if len(parts) <= 1:
        return [], buf
    raw = [p.strip() for p in parts[:-1] if p.strip()]
    sentences, pending = [], ""
    for s in raw:
        if pending:
            s = (pending + " " + s).strip()
            pending = ""
        min_req = _MIN_WORDS_FIRST if not sentences else _MIN_WORDS
        if len(s.split()) < min_req:
            pending = s
            continue
        sentences.append(s)
    remaining = (pending + " " + parts[-1].strip()).strip() if pending else parts[-1].strip()
    return sentences, remaining

def _merge_short(sentences):
    if not sentences:
        return []
    merged, i = [], 0
    while i < len(sentences):
        cur = sentences[i]
        j = i + 1
        while j < len(sentences) and len(sentences[j].split()) <= _MERGE_IF_WORDS:
            cur = (cur + " " + sentences[j]).strip()
            j += 1
        merged.append(cur)
        i = j
    return merged

def _generate_tts_sync(text: str, voice: str, rate: str) -> bytes:
    async def _inner():
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
        parts = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                parts.append(chunk["data"])
        return b"".join(parts)
    return asyncio.run(_inner())

_tts_pool = ThreadPoolExecutor(max_workers=4)

def _stream_generator(session_id: str, chunk_iter, is_realtime: bool, tts_enabled: bool = False):
    yield f"data: {json.dumps({'session_id': session_id, 'chunk': '', 'done': False})}\n\n"

    buffer = ""
    held = None
    is_first = True
    audio_queue = []
    full_text = ""  # Accumulate full response for action tag processing

    def _submit(text):
        if not text or not text.strip():
            return
        # Strip [EMOTION:tag] and [ACTION:...] so they're not spoken aloud
        text = re.sub(r'^\[EMOTION:\w+\]\n?', '', text).strip()
        text = re.sub(r'\[ACTION:\w+\(.*?\)\]', '', text, flags=re.DOTALL).strip()
        if not text:
            return
        audio_queue.append((_tts_pool.submit(_generate_tts_sync, text, config_module.TTS_VOICE, config_module.TTS_RATE), text))

    def _drain_ready():
        events = []
        while audio_queue and audio_queue[0][0].done():
            fut, sent = audio_queue.pop(0)
            try:
                audio = fut.result()
                b64 = base64.b64encode(audio).decode("ascii")
                events.append(f"data: {json.dumps({'audio': b64, 'sentence': sent})}\n\n")
            except Exception as exc:
                logger.warning("[TTS-INLINE] Failed for '%s': %s", sent[:40], exc)
        return events

    try:
        for chunk in chunk_iter:
            if isinstance(chunk, dict) and "_activity" in chunk:
                yield f"data: {json.dumps({'activity': chunk['_activity']})}\n\n"
                continue
            if isinstance(chunk, dict) and "_search_results" in chunk:
                yield f"data: {json.dumps({'search_results': chunk['_search_results']})}\n\n"
                continue
            if not chunk:
                continue

            full_text += chunk
            yield f"data: {json.dumps({'chunk': chunk, 'done': False})}\n\n"

            if not tts_enabled:
                continue

            for ev in _drain_ready():
                yield ev

            buffer += chunk
            sentences, buffer = _split_sentences(buffer)
            sentences = _merge_short(sentences)

            for i, sent in enumerate(sentences):
                min_w = _MIN_WORDS_FIRST if is_first else _MIN_WORDS
                if len(sent.split()) < min_w:
                    # Too short on its own — merge with held
                    if held:
                        held = (held + " " + sent).strip()
                    else:
                        held = sent
                    continue
                is_last = (i == len(sentences) - 1)
                if held:
                    _submit(held)
                    held = None
                    is_first = False
                if is_last:
                    held = sent
                else:
                    _submit(sent)
                    is_first = False

    except Exception as e:
        for fut, _ in audio_queue:
            fut.cancel()
        yield f"data: {json.dumps({'chunk': '', 'done': True, 'error': str(e)})}\n\n"
        return

    # Process [ACTION:...] tags from the full response
    action_results = []
    try:
        cleaned_text, action_results = process_text_for_actions(full_text)
        for ar in action_results:
            yield f"data: {json.dumps({'activity': {'event': 'tool_executed', 'tool': ar['tool'], 'result': ar['result']}})}\n\n"
            logger.info("[TOOL] Executed %s -> %s", ar['tool'], str(ar['result'])[:100])
    except Exception as e:
        logger.warning("[TOOL] Action processing error: %s", e)

    # Send tool results directly to the user (no extra LLM call = instant)
    if action_results and chat_service:
        try:
            # Update the assistant message in history to cleaned text (without action tags)
            if chat_service.sessions.get(session_id):
                chat_service.sessions[session_id][-1].content = cleaned_text

            # Format tool results as a direct, readable response
            result_parts = []
            for ar in action_results:
                result_parts.append(ar['result'])
            direct_text = "\n\n".join(result_parts)

            if direct_text.strip():
                # Send the results as response chunks (appears in chat bubble)
                chunk_size = 40
                for i in range(0, len(direct_text), chunk_size):
                    piece = direct_text[i:i + chunk_size]
                    yield f"data: {json.dumps({'chunk': piece, 'done': False})}\n\n"

                # Update assistant message to include results
                if chat_service.sessions.get(session_id):
                    chat_service.sessions[session_id][-1].content = cleaned_text + "\n\n" + direct_text
                    chat_service.save_chat_session(session_id)

                # TTS for the results
                if tts_enabled:
                    try:
                        _submit(direct_text)
                    except Exception as tts_err:
                        logger.warning("[TOOL-TTS] Error: %s", tts_err)

                logger.info("[TOOL-RESULT] Sent %d chars directly to user", len(direct_text))

        except Exception as e:
            logger.warning("[TOOL-RESULT] Error sending results: %s", e, exc_info=True)

    # --- Process [CREATE_TOOL:...] tags (Runtime Feature Creation) ---
    create_tool_results = []
    try:
        create_pattern = re.compile(r'\[CREATE_TOOL:(.*?)\]', re.DOTALL)
        create_matches = create_pattern.findall(full_text)
        if create_matches:
            from app.services.tools.action_builder import action_builder
            from app.services.tools.custom_action_manager import custom_action_manager
            from app.services.tools.tool_schema import tool_registry
            from config import CURRENT_USER_ID

            for request_desc in create_matches:
                request_desc = request_desc.strip()
                if not request_desc:
                    continue

                logger.info("[CREATE-TOOL] LLM requested tool creation: %s", request_desc[:80])
                yield f"data: {json.dumps({'activity': {'event': 'creating_feature', 'label': 'CREATING NEW CAPABILITY'}})}\n\n"

                # Get existing tool names so LLM doesn't duplicate
                existing_tools = list(tool_registry.all_tools().keys())

                # Ask LLM to generate the action definition
                action_def = action_builder.build_action_from_request(request_desc, existing_tools)
                if not action_def:
                    result_msg = f"Could not create a tool for: {request_desc}"
                    create_tool_results.append({"request": request_desc, "result": result_msg})
                    continue

                # Validate for safety
                is_safe, reason = action_builder.validate_action(action_def)
                if not is_safe:
                    result_msg = f"Cannot create '{action_def.get('name', 'tool')}': {reason}"
                    create_tool_results.append({"request": request_desc, "result": result_msg})
                    continue

                # Create and persist the action
                success, msg = custom_action_manager.create_action(CURRENT_USER_ID, action_def)
                if not success:
                    create_tool_results.append({"request": request_desc, "result": msg})
                    continue

                # Register in the tool registry so LLM knows about it next time
                tool_registry.register_custom_action(action_def)

                # Execute the newly created action immediately
                tool_name = action_def.get("name", "")
                exec_result = custom_action_manager.execute_action(tool_name, [])
                logger.info("[CREATE-TOOL] Created and executed '%s': %s", tool_name, str(exec_result)[:100])

                create_tool_results.append({
                    "request": request_desc,
                    "tool": tool_name,
                    "result": exec_result,
                })

            # Strip [CREATE_TOOL:...] tags from the displayed text
            cleaned_text = create_pattern.sub("", full_text).strip()
            if chat_service.sessions.get(session_id):
                chat_service.sessions[session_id][-1].content = cleaned_text

    except Exception as e:
        logger.warning("[CREATE-TOOL] Error: %s", e, exc_info=True)

    # Send create-tool results to the user
    if create_tool_results:
        try:
            result_parts = []
            for ctr in create_tool_results:
                result_parts.append(ctr.get("result", "Done."))
            direct_text = "\n\n".join(result_parts)

            if direct_text.strip():
                chunk_size = 40
                for i in range(0, len(direct_text), chunk_size):
                    piece = direct_text[i:i + chunk_size]
                    yield f"data: {json.dumps({'chunk': piece, 'done': False})}\n\n"

                # Update session with results
                if chat_service.sessions.get(session_id):
                    current_content = chat_service.sessions[session_id][-1].content or ""
                    chat_service.sessions[session_id][-1].content = current_content + "\n\n" + direct_text
                    chat_service.save_chat_session(session_id)

                # Emit tool_executed activity events
                for ctr in create_tool_results:
                    yield f"data: {json.dumps({'activity': {'event': 'tool_executed', 'tool': ctr.get('tool', 'new_tool'), 'result': ctr.get('result', 'Created')}})}\n\n"

                if tts_enabled:
                    try:
                        _submit(direct_text)
                    except Exception as tts_err:
                        logger.warning("[CREATE-TOOL-TTS] Error: %s", tts_err)

                logger.info("[CREATE-TOOL-RESULT] Sent %d chars to user", len(direct_text))

        except Exception as e:
            logger.warning("[CREATE-TOOL-RESULT] Error: %s", e, exc_info=True)

    if tts_enabled:
        remaining = buffer.strip()
        if held:
            if remaining and len(remaining.split()) <= _MERGE_IF_WORDS:
                _submit((held + " " + remaining).strip())
            else:
                _submit(held)
                if remaining:
                    _submit(remaining)
        elif remaining:
            _submit(remaining)

        for fut, sent in audio_queue:
            try:
                audio = fut.result(timeout=15)
                b64 = base64.b64encode(audio).decode("ascii")
                yield f"data: {json.dumps({'audio': b64, 'sentence': sent})}\n\n"
            except FuturesTimeoutError:
                logger.warning("[TTS-INLINE] Timeout for '%s' (15s)", (sent or "")[:40])
            except Exception as exc:
                logger.warning("[TTS-INLINE] Failed for '%s': %s", (sent or "")[:40], exc)

    # If JARVIS is shutting down, send shutdown event BEFORE done (frontend stops reading at done)
    if _jarvis_shutting_down:
        yield f"data: {json.dumps({'shutdown': True})}\n\n"

    # Always emit the done event (regardless of TTS state)
    yield f"data: {json.dumps({'chunk': '', 'done': True, 'session_id': session_id})}\n\n"

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    if not chat_service:
        raise HTTPException(status_code=503, detail="Chat service not initialized")
    logger.info("[API /chat/stream] Incoming | session_id=%s | message_len=%d | message=%.100s",
                request.session_id or "new", len(request.message), request.message)
    try:
        session_id = chat_service.get_or_create_session(request.session_id)

        user_msg = _inject_file_context(session_id, request.message)

        chunk_iter = chat_service.process_message_stream(session_id, user_msg)
        return StreamingResponse(
            _stream_generator(session_id, chunk_iter, is_realtime=False, tts_enabled=request.tts),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AllGroqApisFailedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        if _is_rate_limit_error(e):
            raise HTTPException(status_code=429, detail=RATE_LIMIT_MESSAGE)
        logger.error("[API /chat/stream] Error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat/realtime", response_model=ChatResponse)
async def chat_realtime(request: ChatRequest):
    if not chat_service:
        raise HTTPException(status_code=503, detail="Chat service not initialized")
    if not realtime_service:
        raise HTTPException(status_code=503, detail="Realtime service not initialized")

    logger.info("[API /chat/realtime] Incoming | session_id=%s | message_len=%d | message=%.100s",
                request.session_id or "new", len(request.message), request.message)
    try:
        session_id = chat_service.get_or_create_session(request.session_id)
        response_text = chat_service.process_realtime_message(session_id, request.message)
        chat_service.save_chat_session(session_id)
        logger.info("[API /chat/realtime] Done | session_id=%s | response_len=%d", session_id[:12], len(response_text))
        return ChatResponse(response=response_text, session_id=session_id)
    except ValueError as e:
        logger.warning("[API /chat/realtime] Invalid session_id: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except AllGroqApisFailedError as e:
        logger.error("[API /chat/realtime] All Groq APIs failed: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        if _is_rate_limit_error(e):
            logger.warning("[API /chat/realtime] Rate limit hit: %s", e)
            raise HTTPException(status_code=429, detail=RATE_LIMIT_MESSAGE)
        logger.error("[API /chat/realtime] Error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing chat: {str(e)}")

@app.post("/chat/realtime/stream")
async def chat_realtime_stream(request: ChatRequest):
    if not chat_service or not realtime_service:
        raise HTTPException(status_code=503, detail="Service not initialized")
    logger.info("[API /chat/realtime/stream] Incoming | session_id=%s | message_len=%d | message=%.100s",
                request.session_id or "new", len(request.message), request.message)
    try:
        session_id = chat_service.get_or_create_session(request.session_id)
        user_msg = _inject_file_context(session_id, request.message)
        
        chunk_iter = chat_service.process_realtime_message_stream(session_id, user_msg)
        return StreamingResponse(
            _stream_generator(session_id, chunk_iter, is_realtime=True, tts_enabled=request.tts),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AllGroqApisFailedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        if _is_rate_limit_error(e):
            raise HTTPException(status_code=429, detail=RATE_LIMIT_MESSAGE)
        logger.error("[API /chat/realtime/stream] Error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat/jarvis/stream")
async def chat_jarvis_stream(request: ChatRequest):
    if not chat_service:
        raise HTTPException(status_code=503, detail="Service not initialized")
    logger.info("[API /chat/jarvis/stream] Incoming | session_id=%s | message_len=%d | message=%.100s",
                request.session_id or "new", len(request.message), request.message)
    try:
        session_id = chat_service.get_or_create_session(request.session_id)
        user_msg = _inject_file_context(session_id, request.message)
        
        chunk_iter = chat_service.process_jarvis_message_stream(session_id, user_msg)
        return StreamingResponse(
            _stream_generator(session_id, chunk_iter, is_realtime=True, tts_enabled=request.tts),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AllGroqApisFailedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        if _is_rate_limit_error(e):
            raise HTTPException(status_code=429, detail=RATE_LIMIT_MESSAGE)
        logger.error("[API /chat/jarvis/stream] Error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/chat/history/{session_id}")
async def get_chat_history(session_id: str):
    if not chat_service:
        raise HTTPException(status_code=503, detail="Chat service not initialized")
    if not chat_service.validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")

    try:
        messages = chat_service.get_chat_history(session_id)
        return {
            "session_id": session_id,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages]
        }
    except Exception as e:
        logger.error(f"Error retrieving history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error retrieving history: {str(e)}")


# ── Session Management Endpoints ──

@app.get("/chat/sessions")
async def list_sessions():
    """List all saved chat sessions with metadata."""
    sessions = []
    try:
        # Sort by filename (encodes creation order) instead of st_mtime to avoid extra stat calls
        files = sorted(config_module.CHATS_DATA_DIR.glob("*.json"), key=lambda p: p.name, reverse=True)
        # Limit to 50 sessions max
        for fp in files[:50]:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                msgs = data.get("messages", [])
                first_msg = ""
                for m in msgs:
                    if isinstance(m, dict) and m.get("role") == "user":
                        first_msg = (m.get("content") or "")[:120]
                        break
                sid = data.get("session_id", fp.stem.replace("chat_", ""))
                sessions.append({
                    "session_id": sid,
                    "preview": first_msg or "(empty session)",
                    "message_count": len(msgs),
                    "modified": fp.stat().st_mtime,
                    "filename": fp.name,
                })
            except Exception as e:
                logger.warning("[API] Could not read session file %s: %s", fp.name, e)
    except Exception as e:
        logger.error("[API] Error listing sessions: %s", e)
    return {"sessions": sessions, "count": len(sessions)}


@app.delete("/chat/session/{session_id}")
async def delete_session(session_id: str):
    """Delete a specific chat session from disk and memory."""
    if not chat_service or not chat_service.validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    safe = session_id.replace("-", "").replace(" ", "_")
    filepath = config_module.CHATS_DATA_DIR / f"chat_{safe}.json"
    deleted = False
    # Remove from memory
    with chat_service._session_lock:
        if session_id in chat_service.sessions:
            del chat_service.sessions[session_id]
            deleted = True
    # Remove from disk
    if filepath.exists():
        try:
            filepath.unlink()
            deleted = True
            logger.info("[API] Deleted session file: %s", filepath.name)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted", "session_id": session_id}


@app.get("/chat/export/{session_id}")
async def export_session(session_id: str, format: str = "md"):
    """Export a chat session as Markdown or plain text."""
    if not chat_service or not chat_service.validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")

    # Try memory first, then disk
    messages = chat_service.get_chat_history(session_id)
    if not messages:
        chat_service.load_session_from_disk(session_id)
        messages = chat_service.get_chat_history(session_id)

    if not messages:
        raise HTTPException(status_code=404, detail="Session not found")

    if format == "md":
        lines = [f"# J.A.R.V.I.S Chat Export\n**Session:** `{session_id}`\n"]
        for msg in messages:
            prefix = "**You:**" if msg.role == "user" else "**J.A.R.V.I.S:**"
            lines.append(f"{prefix} {msg.content}\n")
        content = "\n".join(lines)
        media = "text/markdown"
        ext = "md"
    else:
        lines = [f"JARVIS Chat Export — Session: {session_id}\n{'='*50}\n"]
        for msg in messages:
            prefix = "You:" if msg.role == "user" else "JARVIS:"
            lines.append(f"{prefix} {msg.content}\n")
        content = "\n".join(lines)
        media = "text/plain"
        ext = "txt"

    from starlette.responses import Response
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="jarvis_chat_{session_id[:8]}.{ext}"'}
    )


@app.post("/chat/upload")
async def upload_file(request: FastAPIRequest):
    """Upload any document or code file. Extracts text, indexes into memory."""
    try:
        form = await request.form()
        uploaded = form.get("file")
        if not uploaded or not hasattr(uploaded, "filename"):
            raise HTTPException(status_code=400, detail="No file uploaded. Send as multipart/form-data with key 'file'.")

        filename = uploaded.filename or "upload.txt"
        max_size = 5 * 1024 * 1024  # 5MB
        content_bytes = await uploaded.read()
        if len(content_bytes) > max_size:
            raise HTTPException(status_code=413, detail="File too large (max 5MB)")

        ext = Path(filename).suffix.lower()

        # ── All supported extensions ──
        TEXT_EXTENSIONS = {
            ".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml",
            ".ini", ".cfg", ".env", ".toml",
            ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css",
            ".scss", ".sass", ".less", ".php", ".java", ".c", ".cpp", ".h",
            ".hpp", ".cs", ".go", ".rs", ".rb", ".swift", ".kt", ".sql",
            ".sh", ".bash", ".bat", ".ps1", ".r", ".dart", ".lua", ".vue",
            ".svelte", ".astro", ".graphql", ".gql", ".proto",
            ".rtf", ".tex", ".makefile", ".dockerfile",
        }
        BINARY_EXTENSIONS = {".pdf", ".pptx", ".docx"}
        ALL_ALLOWED = TEXT_EXTENSIONS | BINARY_EXTENSIONS

        if ext not in ALL_ALLOWED:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {ext}. Supported: documents (pdf, pptx, docx), text, and 30+ code formats."
            )

        text = ""
        pages = 0
        slides = 0
        file_type = "text"

        # ── Extract text based on file type ──
        if ext == ".pdf":
            file_type = "pdf"
            try:
                import io
                from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(content_bytes))
                pages = len(reader.pages)
                page_texts = []
                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        page_texts.append(f"--- Page {i+1} ---\n{page_text}")
                text = "\n\n".join(page_texts)
                if not text.strip():
                    raise HTTPException(status_code=400, detail="PDF has no extractable text (might be scanned/image-only).")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to read PDF: {e}")

        elif ext == ".pptx":
            file_type = "pptx"
            try:
                import io
                from pptx import Presentation
                prs = Presentation(io.BytesIO(content_bytes))
                slides = len(prs.slides)
                slide_texts = []
                for i, slide in enumerate(prs.slides):
                    parts = []
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text.strip():
                            parts.append(shape.text.strip())
                    if parts:
                        slide_texts.append(f"--- Slide {i+1} ---\n" + "\n".join(parts))
                    if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                        notes = slide.notes_slide.notes_text_frame.text.strip()
                        if notes:
                            slide_texts.append(f"[Slide {i+1} Notes]: {notes}")
                text = "\n\n".join(slide_texts)
                if not text.strip():
                    raise HTTPException(status_code=400, detail="PowerPoint has no extractable text.")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to read PowerPoint: {e}")

        elif ext == ".docx":
            file_type = "docx"
            try:
                import io
                from docx import Document as DocxDocument
                doc = DocxDocument(io.BytesIO(content_bytes))
                paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                for table in doc.tables:
                    for row in table.rows:
                        row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                        if row_text:
                            paragraphs.append(row_text)
                text = "\n".join(paragraphs)
                pages = max(1, len(text) // 3000)
                if not text.strip():
                    raise HTTPException(status_code=400, detail="Word document has no extractable text.")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to read Word document: {e}")

        else:
            file_type = "code" if ext in {
                ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css",
                ".scss", ".php", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
                ".go", ".rs", ".rb", ".swift", ".kt", ".sql", ".sh", ".bash",
                ".bat", ".ps1", ".r", ".dart", ".lua", ".vue", ".svelte",
            } else "text"
            try:
                text = content_bytes.decode("utf-8", errors="replace")
            except Exception:
                raise HTTPException(status_code=400, detail="Could not decode file as text")

        if not text.strip():
            raise HTTPException(status_code=400, detail="File appears to be empty.")

        # ── Save to learning data ──
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
        dest = config_module.LEARNING_DATA_DIR / safe_name
        dest.write_text(text, encoding="utf-8")

        word_count = len(text.split())
        logger.info("[UPLOAD] Saved %s: %s (%d words, %d chars)", file_type, safe_name, word_count, len(text))

        # ── Index into vector store (background to avoid timeout) ──
        indexed = False
        if vector_store_service:
            try:
                from langchain_core.documents import Document
                doc = Document(page_content=text, metadata={"source": f"upload_{safe_name}", "type": file_type})
                vector_store_service.add_documents([doc])
                indexed = True
                logger.info("[UPLOAD] Indexed into vector store (synchronous)")
            except Exception as idx_err:
                logger.warning("[UPLOAD] Indexing failed (file still saved): %s", idx_err)

        # ── Build preview ──
        preview = text[:300].strip()
        if len(text) > 300:
            preview += "..."

        # ── Store context for next chat message ──
        # Truncate to ~8000 chars to fit in LLM context window
        session_id = form.get("session_id") or "global"
        upload_context_text = text[:8000]
        uploaded_file_context[session_id] = {
            "filename": safe_name,
            "content": upload_context_text,
            "file_type": file_type,
            "words": word_count,
        }
        # Also store under "global" so any session can access it
        uploaded_file_context["global"] = uploaded_file_context[session_id]
        logger.info("[UPLOAD] Stored context for session %s (%d chars)", session_id[:12], len(upload_context_text))

        # ── Store in persistent file database (for multi-message reference) ──
        uploaded_files_db[safe_name] = {
            "filename": safe_name,
            "original_name": filename,
            "content": text,  # Store full content, not truncated
            "file_type": file_type,
            "words": word_count,
            "uploaded_at": datetime.now().isoformat(),
            "session_id": session_id,
        }
        logger.info("[UPLOAD] Added to persistent file database: %s", safe_name)
        _save_app_state()

        return {
            "status": "ok",
            "filename": safe_name,
            "file_type": file_type,
            "chars": len(text),
            "words": word_count,
            "pages": pages if pages else None,
            "slides": slides if slides else None,
            "preview": preview,
            "indexed": indexed,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[UPLOAD] Unexpected error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


# ── Voice Transcription (Groq Whisper) ──

@app.post("/api/transcribe")
async def transcribe_audio(request: FastAPIRequest):
    """
    Transcribe audio using Groq's Whisper API (whisper-large-v3-turbo).
    Accepts audio file as multipart/form-data with key 'audio'.
    Returns {"text": "transcribed text"}.
    """
    try:
        form = await request.form()
        audio_file = form.get("audio")
        if not audio_file or not hasattr(audio_file, "read"):
            raise HTTPException(status_code=400, detail="No audio file. Send as multipart/form-data with key 'audio'.")

        audio_bytes = await audio_file.read()
        if len(audio_bytes) < 100:
            raise HTTPException(status_code=400, detail="Audio file is too small / empty.")
        if len(audio_bytes) > 25 * 1024 * 1024:  # 25MB Groq limit
            raise HTTPException(status_code=413, detail="Audio file too large (max 25MB).")

        # Determine the filename extension for Groq (it needs a real extension)
        original_name = getattr(audio_file, "filename", "audio.webm") or "audio.webm"
        # Groq Whisper accepts: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, flac
        logger.info("[TRANSCRIBE] Received audio: %s (%d bytes)", original_name, len(audio_bytes))

        # Try each Groq API key until one succeeds (rate-limit rotation)
        from groq import Groq
        import io

        last_error = None
        for api_key in GROQ_API_KEYS:
            try:
                client = Groq(api_key=api_key)
                # Create a file-like object with the correct name for Groq
                audio_io = io.BytesIO(audio_bytes)
                audio_io.name = original_name  # Groq uses .name to detect format

                transcription = client.audio.transcriptions.create(
                    file=audio_io,
                    model="whisper-large-v3-turbo",
                    language="en",
                    response_format="text",
                )

                transcript_text = str(transcription).strip()
                if not transcript_text:
                    logger.warning("[TRANSCRIBE] Empty transcript from Groq")
                    return {"text": "", "status": "empty"}

                logger.info("[TRANSCRIBE] Success (%d chars): %.100s", len(transcript_text), transcript_text)
                return {"text": transcript_text, "status": "ok"}

            except Exception as e:
                last_error = e
                err_msg = str(e).lower()
                if "429" in str(e) or "rate limit" in err_msg or "tokens per day" in err_msg:
                    logger.warning("[TRANSCRIBE] Rate limit on key ...%s, trying next", api_key[-6:])
                    continue
                else:
                    # Non-rate-limit error — don't try other keys
                    logger.error("[TRANSCRIBE] Groq error: %s", e)
                    raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")

        # All keys exhausted
        logger.error("[TRANSCRIBE] All Groq API keys exhausted: %s", last_error)
        raise HTTPException(status_code=429, detail="All API keys have reached their rate limit. Please try again later.")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[TRANSCRIBE] Unexpected error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Transcription error: {str(e)}")


# ── File Management Endpoints ──

@app.get("/chat/files")
async def list_uploaded_files():
    """List all uploaded files with their metadata."""
    files = []
    for filename, data in uploaded_files_db.items():
        files.append({
            "filename": data["filename"],
            "original_name": data["original_name"],
            "file_type": data["file_type"],
            "words": data["words"],
            "uploaded_at": data["uploaded_at"],
            "chars": len(data["content"]),
        })
    return {"status": "ok", "files": files, "count": len(files)}


@app.get("/chat/files/{filename}")
async def get_file_content(filename: str):
    """Get the content of a specific uploaded file."""
    if filename not in uploaded_files_db:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    data = uploaded_files_db[filename]
    return {
        "status": "ok",
        "filename": data["filename"],
        "original_name": data["original_name"],
        "file_type": data["file_type"],
        "words": data["words"],
        "content": data["content"],
        "uploaded_at": data["uploaded_at"],
    }


@app.delete("/chat/files/{filename}")
async def delete_uploaded_file(filename: str):
    """Delete an uploaded file from the database."""
    if filename not in uploaded_files_db:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    
    # Remove from database
    del uploaded_files_db[filename]
    _save_app_state()
    
    # Also remove from in-memory context if present
    for key in list(uploaded_file_context.keys()):
        if uploaded_file_context.get(key, {}).get("filename") == filename:
            del uploaded_file_context[key]
    
    logger.info("[FILE] Deleted: %s", filename)
    return {"status": "ok", "message": f"File deleted: {filename}"}


@app.post("/chat/files/{filename}/use")
async def use_file_in_context(filename: str, request: ChatRequest):
    """Explicitly use a specific file in the next chat message."""
    if filename not in uploaded_files_db:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    
    data = uploaded_files_db[filename]
    session_id = request.session_id or "global"
    
    # Store in session context for next message
    uploaded_file_context[session_id] = {
        "filename": data["filename"],
        "content": data["content"][:8000],  # Truncate for context window
        "file_type": data["file_type"],
        "words": data["words"],
    }
    uploaded_file_context["global"] = uploaded_file_context[session_id]
    
    logger.info("[FILE] Added to context for session %s: %s", session_id[:12], filename)
    return {"status": "ok", "message": f"File '{filename}' will be used in next message"}


# ── File Summarization Endpoint ──

@app.post("/chat/files/{filename}/summarize")
async def summarize_file(filename: str):
    """Generate a summary of an uploaded file using AI."""
    if filename not in uploaded_files_db:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    
    data = uploaded_files_db[filename]
    content = data["content"]
    
    # Truncate content if too long for summarization
    max_chars = 15000
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n[Content truncated for summarization]"
    
    # Use Groq to summarize
    if not groq_service:
        raise HTTPException(status_code=503, detail="AI service not available")
    
    try:
        # Use LangChain ChatGroq with key rotation fallback
        summary = None
        last_exc = None
        for llm in groq_service.llms:
            try:
                response = llm.invoke([
                    SystemMessage(content="You are a helpful assistant that creates concise summaries of documents. Provide a clear, well-structured summary with key points."),
                    HumanMessage(content=f"Please summarize the following document:\n\n{content}")
                ])
                summary = response.content
                break
            except Exception as key_exc:
                last_exc = key_exc
                err_msg = str(key_exc).lower()
                if "429" in str(key_exc) or "rate limit" in err_msg:
                    logger.warning("[SUMMARIZE] Rate limit on key, trying next")
                    continue
                else:
                    raise
        if summary is None:
            raise Exception(f"All API keys failed: {last_exc}")

        return {
            "status": "ok",
            "filename": filename,
            "original_name": data["original_name"],
            "summary": summary,
            "word_count": data["words"],
        }
    except Exception as e:
        logger.error("[SUMMARIZE] Failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Summarization failed: {str(e)}")


# ── Multi-file Chat Endpoint ──

class MultiFileChatRequest(BaseModel):
    message: str
    filenames: list[str] = []  # List of filenames to include
    session_id: str = None
    tts: bool = False

@app.post("/chat/multifile")
async def chat_multifile(request: MultiFileChatRequest):
    """Chat with multiple uploaded files at once."""
    if not chat_service:
        raise HTTPException(status_code=503, detail="Chat service not initialized")
    
    session_id = chat_service.get_or_create_session(request.session_id)
    
    # Build context from multiple files
    context_parts = []
    for filename in request.filenames:
        if filename in uploaded_files_db:
            data = uploaded_files_db[filename]
            context_parts.append(
                f"[File: {data['original_name']} ({data['file_type']}, {data['words']} words)]\n"
                f"--- CONTENT ---\n{data['content'][:5000]}\n--- END CONTENT ---"
            )
    
    # Also search vector store for relevant content
    vector_results = search_vector_store_for_files(request.message, top_k=3)
    for result in vector_results:
        context_parts.append(
            f"[Relevant from knowledge base: {result['source']}]\n{result['content'][:2000]}"
        )
    
    if context_parts:
        user_msg = (
            f"{chr(10).join(context_parts)}\n\n"
            f"User's question: {request.message}"
        )
    else:
        user_msg = request.message
    
    chunk_iter = chat_service.process_message_stream(session_id, user_msg)
    return StreamingResponse(
        _stream_generator(session_id, chunk_iter, is_realtime=False, tts_enabled=request.tts),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Reminder System Endpoints ──

class ReminderCreate(BaseModel):
    message: str
    remind_at: str  # ISO datetime string or datetime-local format
    repeat: str = "none"  # none, daily, weekly, monthly
    sound: str = "default"  # default, bell, alarm, chime
    speak: bool = False
    active: bool = True

def parse_datetime(dt_str: str) -> datetime:
    """Parse datetime string with flexible formats."""
    # Handle datetime-local format (YYYY-MM-DDTHH:MM)
    if 'T' not in dt_str and ' ' not in dt_str:
        # Date-only string (e.g. "2026-04-25") — assume midnight
        dt_str = dt_str + "T00:00"
    # Handle timezone info
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        # Try with space separator
        try:
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError(f"Cannot parse datetime: {dt_str}")

@app.post("/chat/reminders")
async def create_reminder(body: ReminderCreate):
    """Create a new reminder."""
    try:
        remind_dt = parse_datetime(body.remind_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {str(e)}. Use format like 2026-04-25T14:30 or 2026-04-25 14:30")
    
    reminder_id = str(uuid.uuid4())[:8]
    reminder = {
        "id": reminder_id,
        "message": body.message,
        "remind_at": remind_dt.isoformat(),
        "repeat": body.repeat,
        "sound": body.sound,
        "speak": body.speak,
        "active": body.active,
        "created_at": datetime.now().isoformat(),
    }
    reminders_db[reminder_id] = reminder
    _save_app_state()
    
    logger.info("[REMINDER] Created: %s at %s", reminder_id, remind_dt)
    return {"status": "ok", "reminder": reminder}

@app.get("/chat/reminders")
async def list_reminders():
    """List all reminders."""
    reminders = []
    for rid, data in reminders_db.items():
        reminders.append({
            "id": rid,
            "message": data["message"],
            "remind_at": data["remind_at"],
            "repeat": data["repeat"],
            "sound": data["sound"],
            "speak": data["speak"],
            "active": data["active"],
        })
    return {"status": "ok", "reminders": reminders, "count": len(reminders)}

@app.delete("/chat/reminders/{reminder_id}")
async def delete_reminder(reminder_id: str):
    """Delete a reminder."""
    if reminder_id not in reminders_db:
        raise HTTPException(status_code=404, detail=f"Reminder not found: {reminder_id}")
    
    del reminders_db[reminder_id]
    _save_app_state()
    return {"status": "ok", "message": f"Reminder deleted: {reminder_id}"}

@app.put("/chat/reminders/{reminder_id}")
async def update_reminder(reminder_id: str, body: ReminderCreate):
    """Update a reminder."""
    if reminder_id not in reminders_db:
        raise HTTPException(status_code=404, detail=f"Reminder not found: {reminder_id}")
    
    try:
        remind_dt = datetime.fromisoformat(body.remind_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datetime format.")
    
    reminders_db[reminder_id] = {
        "id": reminder_id,
        "message": body.message,
        "remind_at": remind_dt.isoformat(),
        "repeat": body.repeat,
        "sound": body.sound,
        "speak": body.speak,
        "active": body.active,
        "created_at": reminders_db[reminder_id].get("created_at", datetime.now().isoformat()),
    }
    _save_app_state()
    return {"status": "ok", "reminder": reminders_db[reminder_id]}

@app.get("/chat/reminders/next")
async def get_next_reminder():
    """Get the next upcoming reminder."""
    now = datetime.now()
    next_reminder = None
    
    for rid, data in reminders_db.items():
        if not data.get("active", True):
            continue
        remind_dt = datetime.fromisoformat(data["remind_at"])
        if remind_dt > now:
            if next_reminder is None or remind_dt < datetime.fromisoformat(next_reminder["remind_at"]):
                next_reminder = {"id": rid, **data}
    
    return {"status": "ok", "reminder": next_reminder}


# ── AI-Powered Reminder Creation ──

class AIReminderRequest(BaseModel):
    message: str  # Natural language like "Remind me to call mom tomorrow at 2pm"

@app.post("/chat/reminders/ai")
async def create_reminder_ai(body: AIReminderRequest):
    """Create a reminder from natural language using AI."""
    if not groq_service:
        raise HTTPException(status_code=503, detail="AI service not available")
    
    try:
        # Use LangChain ChatGroq with key rotation fallback
        ai_response_content = None
        last_exc = None
        reminder_system_prompt = """You are a reminder parser. Extract the reminder details from the user's message.
Return a JSON object with these fields:
- message: The reminder message (what to remind)
- remind_at: The datetime in ISO format (YYYY-MM-DDTHH:MM) - use current date as reference
- repeat: "none", "daily", "weekly", or "monthly" based on the message
- sound: "default" unless specified
- speak: false unless voice is requested

Examples:
- "Remind me to call mom tomorrow at 2pm" -> {"message": "Call mom", "remind_at": "2026-04-26T14:00", "repeat": "none", "sound": "default", "speak": false}
- "Remind me to drink water every day at 9am" -> {"message": "Drink water", "remind_at": "2026-04-25T09:00", "repeat": "daily", "sound": "default", "speak": false}
- "Meeting with team every Monday at 3pm" -> {"message": "Meeting with team", "remind_at": "2026-04-27T15:00", "repeat": "weekly", "sound": "default", "speak": false}

Current date and time: """ + datetime.now().strftime("%Y-%m-%d %H:%M") + """
Only return valid JSON, no other text."""

        for llm in groq_service.llms:
            try:
                response = llm.invoke([
                    SystemMessage(content=reminder_system_prompt),
                    HumanMessage(content=body.message)
                ])
                ai_response_content = response.content
                break
            except Exception as key_exc:
                last_exc = key_exc
                err_msg = str(key_exc).lower()
                if "429" in str(key_exc) or "rate limit" in err_msg:
                    logger.warning("[REMINDER] Rate limit on key, trying next")
                    continue
                else:
                    raise
        if ai_response_content is None:
            raise Exception(f"All API keys failed: {last_exc}")

        result = json.loads(ai_response_content)
        
        # Create the reminder
        reminder_id = str(uuid.uuid4())[:8]
        remind_dt = parse_datetime(result.get("remind_at", datetime.now().isoformat()))
        
        reminder = {
            "id": reminder_id,
            "message": result.get("message", body.message),
            "remind_at": remind_dt.isoformat(),
            "repeat": result.get("repeat", "none"),
            "sound": result.get("sound", "default"),
            "speak": result.get("speak", False),
            "active": True,
            "created_at": datetime.now().isoformat(),
        }
        reminders_db[reminder_id] = reminder
        _save_app_state()
        
        logger.info("[REMINDER] AI created: %s at %s", reminder_id, remind_dt)
        return {"status": "ok", "reminder": reminder, "parsed": result}
        
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Could not parse AI response. Please try again.")
    except Exception as e:
        logger.error("[REMINDER] AI creation failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to create reminder: {str(e)}")


# ── Webhook Endpoints ──

def _validate_webhook_url(url: str) -> bool:
    """Validate webhook URL to prevent SSRF attacks.
    Must be HTTPS and must not resolve to localhost, private, or link-local IPs.
    Returns True if valid, raises HTTPException if not.
    """
    import urllib.parse
    import socket
    import ipaddress

    parsed = urllib.parse.urlparse(url)

    # Must use HTTPS
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Webhook URL must use HTTPS.")

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Webhook URL has no valid hostname.")

    # Resolve hostname and check for private/internal IPs
    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
        for family, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
                raise HTTPException(
                    status_code=400,
                    detail=f"Webhook URL resolves to a blocked IP address ({ip}). "
                           "Internal/private addresses are not allowed."
                )
    except HTTPException:
        raise
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"Cannot resolve webhook hostname: {hostname}")

    return True

class WebhookCreate(BaseModel):
    url: str
    events: list[str] = ["chat_completed"]  # chat_completed, reminder_triggered, file_uploaded
    active: bool = True

@app.post("/webhooks")
async def create_webhook(body: WebhookCreate):
    """Create a new webhook for notifications."""
    # Validate URL to prevent SSRF
    _validate_webhook_url(body.url)

    webhook_id = str(uuid.uuid4())[:8]
    webhook = {
        "id": webhook_id,
        "url": body.url,
        "events": body.events,
        "active": body.active,
        "created_at": datetime.now().isoformat(),
    }
    webhooks_db[webhook_id] = webhook
    _save_app_state()
    logger.info("[WEBHOOK] Created: %s -> %s", webhook_id, body.url)
    return {"status": "ok", "webhook": webhook}

@app.get("/webhooks")
async def list_webhooks():
    """List all webhooks."""
    webhooks = []
    for wid, data in webhooks_db.items():
        webhooks.append({
            "id": wid,
            "url": data["url"],
            "events": data["events"],
            "active": data["active"],
        })
    return {"status": "ok", "webhooks": webhooks, "count": len(webhooks)}

@app.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: str):
    """Delete a webhook."""
    if webhook_id not in webhooks_db:
        raise HTTPException(status_code=404, detail=f"Webhook not found: {webhook_id}")
    
    del webhooks_db[webhook_id]
    _save_app_state()
    return {"status": "ok", "message": f"Webhook deleted: {webhook_id}"}

async def trigger_webhook(event: str, data: dict):
    """Trigger all webhooks for a specific event."""
    for wid, webhook in webhooks_db.items():
        if not webhook.get("active", True):
            continue
        if event in webhook.get("events", []):
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    await session.post(webhook["url"], json={"event": event, "data": data})
                logger.info("[WEBHOOK] Triggered: %s for event %s", wid, event)
            except Exception as e:
                logger.warning("[WEBHOOK] Failed for %s: %s", wid, e)


# ── Email Configuration Endpoints ──

class EmailConfigUpdate(BaseModel):
    smtp_host: str = None
    smtp_port: int = None
    username: str = None
    password: str = None
    from_email: str = None

@app.get("/integrations/email/config")
async def get_email_config():
    """Get email configuration (without password)."""
    return {
        "status": "ok",
        "configured": bool(email_config.get("smtp_host")),
        "smtp_host": email_config.get("smtp_host", ""),
        "smtp_port": email_config.get("smtp_port", 587),
        "username": email_config.get("username", ""),
        "from_email": email_config.get("from_email", ""),
    }

@app.post("/integrations/email/config")
async def update_email_config(body: EmailConfigUpdate):
    """Update email configuration."""
    if body.smtp_host is not None:
        email_config["smtp_host"] = body.smtp_host
    if body.smtp_port is not None:
        email_config["smtp_port"] = body.smtp_port
    if body.username is not None:
        email_config["username"] = body.username
    if body.password is not None:
        email_config["password"] = body.password
    if body.from_email is not None:
        email_config["from_email"] = body.from_email
    
    logger.info("[EMAIL] Config updated")
    _save_app_state()
    return {"status": "ok", "message": "Email configuration updated"}

class EmailSendRequest(BaseModel):
    to: str
    subject: str
    body: str

@app.post("/integrations/email/send")
async def send_email(body: EmailSendRequest):
    """Send an email."""
    if not email_config.get("smtp_host"):
        raise HTTPException(status_code=400, detail="Email not configured")
    
    try:
        import aiosmtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        msg = MIMEMultipart()
        msg["From"] = email_config.get("from_email", email_config.get("username"))
        msg["To"] = body.to
        msg["Subject"] = body.subject
        msg.attach(MIMEText(body.body, "plain"))
        
        await aiosmtplib.send(
            msg,
            hostname=email_config["smtp_host"],
            port=email_config["smtp_port"],
            username=email_config["username"],
            password=email_config["password"],
        )
        
        logger.info("[EMAIL] Sent to %s", body.to)
        return {"status": "ok", "message": "Email sent successfully"}
    except ImportError:
        raise HTTPException(status_code=501, detail="Email library not installed. Run: pip install aiosmtplib")
    except Exception as e:
        logger.error("[EMAIL] Send failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")


# ── Notion Integration Endpoints ──

class NotionConfigUpdate(BaseModel):
    api_key: str = None
    database_id: str = None

@app.get("/integrations/notion/config")
async def get_notion_config():
    """Get Notion configuration."""
    return {
        "status": "ok",
        "configured": bool(notion_config.get("api_key")),
        "database_id": notion_config.get("database_id", ""),
    }

@app.post("/integrations/notion/config")
async def update_notion_config(body: NotionConfigUpdate):
    """Update Notion configuration."""
    if body.api_key is not None:
        notion_config["api_key"] = body.api_key
    if body.database_id is not None:
        notion_config["database_id"] = body.database_id
    
    logger.info("[NOTION] Config updated")
    _save_app_state()
    return {"status": "ok", "message": "Notion configuration updated"}

@app.get("/integrations/notion/pages")
async def list_notion_pages():
    """List pages from Notion database."""
    if not notion_config.get("api_key"):
        raise HTTPException(status_code=400, detail="Notion not configured")
    
    try:
        from notion_client import AsyncClient
        notion = AsyncClient(auth=notion_config["api_key"])
        response = await notion.databases.query(notion_config["database_id"])
        
        pages = []
        for page in response.get("results", []):
            title = "Untitled"
            if "Name" in page["properties"]:
                title = page["properties"]["Name"].get("title", [{}])[0].get("plain_text", "Untitled")
            pages.append({
                "id": page["id"],
                "title": title,
                "url": page.get("url", ""),
            })
        return {"status": "ok", "pages": pages, "count": len(pages)}
    except ImportError:
        raise HTTPException(status_code=501, detail="Notion client not installed. Run: pip install notion")
    except Exception as e:
        logger.error("[NOTION] Failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Notion API error: {str(e)}")

@app.post("/integrations/notion/pages")
async def create_notion_page(body: dict):
    """Create a new page in Notion."""
    if not notion_config.get("api_key"):
        raise HTTPException(status_code=400, detail="Notion not configured")
    
    try:
        from notion_client import AsyncClient
        notion = AsyncClient(auth=notion_config["api_key"])
        
        page = await notion.pages.create(
            parent={"database_id": notion_config["database_id"]},
            properties=body.get("properties", {}),
        )
        
        logger.info("[NOTION] Created page: %s", page["id"])
        return {"status": "ok", "page_id": page["id"], "url": page.get("url", "")}
    except ImportError:
        raise HTTPException(status_code=501, detail="Notion client not installed. Run: pip install notion")
    except Exception as e:
        logger.error("[NOTION] Create failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Notion API error: {str(e)}")


# ── Google Calendar Integration Endpoints ──

class CalendarConfigUpdate(BaseModel):
    credentials_json: str = None  # Base64 encoded or raw JSON

# Google OAuth settings
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8000/integrations/calendar/callback")

@app.get("/integrations/calendar/config")
async def get_calendar_config():
    """Get Calendar configuration status."""
    return {
        "status": "ok",
        "configured": bool(calendar_config.get("token_json")),
    }

@app.post("/integrations/calendar/config")
async def update_calendar_config(body: CalendarConfigUpdate):
    """Update Google Calendar configuration with credentials JSON."""
    if body.credentials_json is not None:
        calendar_config["credentials_json"] = body.credentials_json
    
    logger.info("[CALENDAR] Config updated")
    _save_app_state()
    return {"status": "ok", "message": "Calendar configuration updated"}

@app.get("/integrations/calendar/auth")
async def calendar_auth():
    """Start Google OAuth flow."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=400, detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID environment variable.")
    
    scopes = ["https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/calendar.events"]
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?client_id={GOOGLE_CLIENT_ID}&redirect_uri={GOOGLE_REDIRECT_URI}&response_type=code&scope={'+'.join(scopes)}&access_type=offline&prompt=consent"
    
    return RedirectResponse(url=auth_url)

@app.get("/integrations/calendar/callback")
async def calendar_callback(code: str = None, error: str = None):
    """Handle OAuth callback."""
    if error:
        return {"status": "error", "message": f"Authorization failed: {error}"}
    
    if not code:
        return {"status": "error", "message": "No authorization code received"}
    
    try:
        # Exchange code for tokens
        import urllib.parse
        import urllib.request
        token_data = urllib.parse.urlencode({
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_REDIRECT_URI,
        }).encode()
        
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=token_data, method="POST")
        with urllib.request.urlopen(req) as response:
            tokens = json.loads(response.read().decode())
        
        calendar_config["token_json"] = json.dumps(tokens)
        _save_app_state()
        logger.info("[CALENDAR] OAuth successful")
        
        return {"status": "ok", "message": "Google Calendar connected successfully!"}
        
    except Exception as e:
        logger.error("[CALENDAR] OAuth failed: %s", e)
        return {"status": "error", "message": f"Failed to complete authorization: {str(e)}"}

@app.get("/integrations/calendar/events")
async def list_calendar_events(start_date: str = None, end_date: str = None):
    """List events from Google Calendar."""
    if not calendar_config.get("token_json"):
        raise HTTPException(status_code=400, detail="Calendar not configured. Click 'Connect' to authorize.")
    
    try:
        import urllib.request
        tokens = json.loads(calendar_config["token_json"])
        access_token = tokens.get("access_token")
        
        if not access_token:
            raise HTTPException(status_code=400, detail="Invalid token. Please reconnect.")
        
        # Get date range (default: next 7 days)
        if not start_date:
            start_date = datetime.now().isoformat()
        if not end_date:
            end_date = (datetime.now() + timedelta(days=7)).isoformat()
        
        # Call Google Calendar API
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin={start_date}&timeMax={end_date}&singleEvents=true&orderBy=startTime"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
        
        events = []
        for event in data.get("items", []):
            start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
            end = event.get("end", {}).get("dateTime", event.get("end", {}).get("date", ""))
            events.append({
                "id": event.get("id"),
                "summary": event.get("summary", "No title"),
                "start": start,
                "end": end,
                "htmlLink": event.get("htmlLink", ""),
            })
        
        return {"status": "ok", "events": events, "count": len(events)}
        
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Token expired, try to refresh
            try:
                import urllib.parse
                tokens = json.loads(calendar_config["token_json"])
                refresh_token = tokens.get("refresh_token")
                
                if refresh_token:
                    token_data = urllib.parse.urlencode({
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    }).encode()
                    
                    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=token_data, method="POST")
                    with urllib.request.urlopen(req) as response:
                        new_tokens = json.loads(response.read().decode())
                    
                    calendar_config["token_json"] = json.dumps({**tokens, **new_tokens})
                    _save_app_state()
                    return await list_calendar_events(start_date, end_date)
            except:
                calendar_config["token_json"] = ""
                _save_app_state()
                raise HTTPException(status_code=400, detail="Calendar token expired. Please reconnect.")
        raise HTTPException(status_code=500, detail=f"Calendar API error: {str(e)}")
    except Exception as e:
        logger.error("[CALENDAR] Events failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to get events: {str(e)}")

class CalendarEventCreate(BaseModel):
    summary: str
    description: str = ""
    start: str  # ISO datetime
    end: str    # ISO datetime
    attendees: list[str] = []

@app.post("/integrations/calendar/events")
async def create_calendar_event(body: CalendarEventCreate):
    """Create a new calendar event."""
    if not calendar_config.get("token_json"):
        raise HTTPException(status_code=400, detail="Calendar not configured. Click 'Connect' to authorize.")
    
    try:
        import urllib.request
        tokens = json.loads(calendar_config["token_json"])
        access_token = tokens.get("access_token")
        
        event_data = {
            "summary": body.summary,
            "description": body.description,
            "start": {"dateTime": body.start, "timeZone": "UTC"},
            "end": {"dateTime": body.end, "timeZone": "UTC"},
        }
        
        if body.attendees:
            event_data["attendees"] = [{"email": email} for email in body.attendees]
        
        json_data = json.dumps(event_data).encode()
        req = urllib.request.Request(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            data=json_data,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            method="POST"
        )
        
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
        
        logger.info("[CALENDAR] Created event: %s", result.get("id"))
        return {"status": "ok", "event": result}
        
    except Exception as e:
        logger.error("[CALENDAR] Create failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to create event: {str(e)}")


# ── Slack Bot Endpoints ──

class SlackConfigUpdate(BaseModel):
    bot_token: str = None
    signing_secret: str = None
    app_token: str = None  # For Socket Mode

@app.get("/integrations/slack/config")
async def get_slack_config():
    """Get Slack configuration status."""
    return {
        "status": "ok",
        "configured": bool(slack_config.get("bot_token")),
    }

@app.post("/integrations/slack/config")
async def update_slack_config(body: SlackConfigUpdate):
    """Update Slack configuration."""
    if body.bot_token is not None:
        slack_config["bot_token"] = body.bot_token
    if body.signing_secret is not None:
        slack_config["signing_secret"] = body.signing_secret
    if body.app_token is not None:
        slack_config["app_token"] = body.app_token
    
    logger.info("[SLACK] Config updated")
    _save_app_state()
    return {"status": "ok", "message": "Slack configuration updated"}

@app.post("/integrations/slack/events")
async def slack_events(request: Request):
    """Handle Slack events (slash commands, messages)."""
    if not slack_config.get("bot_token"):
        raise HTTPException(status_code=400, detail="Slack not configured")
    
    # This would handle Slack webhook events
    # Full implementation requires slack-sdk
    body = await request.body()
    logger.info("[SLACK] Event received")
    return {"status": "ok"}


@app.get("/settings")
async def get_settings():
    return {
        "user_title": config_module.JARVIS_USER_TITLE,
        "tts_voice": config_module.TTS_VOICE,
        "tts_rate": config_module.TTS_RATE,
        "owner_name": getattr(config_module, "JARVIS_OWNER_NAME", ""),
    }

@app.post("/settings")
async def update_settings(body: SettingsUpdate):
    updated = []
    if body.user_title is not None:
        config_module.JARVIS_USER_TITLE = body.user_title.strip()
        updated.append("user_title")
    if body.tts_voice is not None:
        config_module.TTS_VOICE = body.tts_voice.strip()
        updated.append("tts_voice")
    if body.tts_rate is not None:
        config_module.TTS_RATE = body.tts_rate.strip()
        updated.append("tts_rate")
    # Rebuild system prompt with new settings
    base = config_module._JARVIS_SYSTEM_PROMPT_BASE.format(assistant_name=config_module.ASSISTANT_NAME)
    parts = []
    if config_module.JARVIS_USER_TITLE:
        parts.append(f"\n- When appropriate, you may address the user as: {config_module.JARVIS_USER_TITLE}")
    if getattr(config_module, "JARVIS_OWNER_NAME", ""):
        parts.append(f"\n- Your owner/creator is: {config_module.JARVIS_OWNER_NAME}. You serve {config_module.JARVIS_OWNER_NAME}.")
    try:
        tools_desc = config_module._TOOLS_DESCRIPTION
    except AttributeError:
        tools_desc = ""
    config_module.JARVIS_SYSTEM_PROMPT = base + "".join(parts) + tools_desc
    logger.info("[SETTINGS] Updated: %s", ", ".join(updated) if updated else "(none)")
    return {"status": "ok", "updated": updated}

@app.post("/tts")
async def text_to_speech(request_body: TTSRequest):
    text = request_body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    async def generate():
        try:
            communicate = edge_tts.Communicate(text=text, voice=config_module.TTS_VOICE, rate=config_module.TTS_RATE)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
        except Exception as e:
            logger.error("[TTS] Error generating speech: %s", e)

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-cache"},
    )

# =============================================================================
# DEVICE WEBSOCKET ENDPOINT
# =============================================================================
# Real devices (Android app, ESP32 glasses) connect here.
# Each device registers with DeviceManager and can receive tool calls.

from app.services.device_manager import device_manager, DeviceSession

_active_device_websockets: dict = {}  # device_id -> WebSocket


@app.websocket("/ws/device/{device_id}")
async def device_websocket(websocket: WebSocket, device_id: str):
    """
    WebSocket endpoint for device connections.
    Protocol:
      1. Device connects -> registers with DeviceManager
      2. Device sends heartbeat every N seconds
      3. Server can send tool commands
      4. Device executes and returns result
    Message types (client -> server):
      {"type": "register", "device_type": "android", "capabilities": [...], "metadata": {...}}
      {"type": "heartbeat", "battery": 78, "network": "wifi"}
      {"type": "tool_result", "request_id": "abc", "result": "..."}
      {"type": "unregister"}
    Message types (server -> client):
      {"type": "tool_call", "request_id": "abc", "tool": "...", "params": {...}}
      {"type": "ack", "device_id": "..."}
      {"type": "ping"}
    """
    await websocket.accept()
    device = None
    logger.info("[WS-DEVICE] %s connected", device_id)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "")

            if msg_type == "register":
                device = DeviceSession(
                    device_id=device_id,
                    device_type=msg.get("device_type", "unknown"),
                    capabilities=msg.get("capabilities", []),
                    permissions=msg.get("permissions", {}),
                    battery=msg.get("battery"),
                    network=msg.get("network", "unknown"),
                    status="online",
                    metadata=msg.get("metadata", {}),
                    websocket=websocket,
                )
                device_manager.register(device)
                _active_device_websockets[device_id] = websocket
                await websocket.send_json({"type": "ack", "device_id": device_id})
                logger.info("[WS-DEVICE] %s registered: type=%s caps=%s",
                            device_id, device.device_type, device.capabilities)

            elif msg_type == "heartbeat":
                if device:
                    device_manager.heartbeat(
                        device_id,
                        battery=msg.get("battery"),
                        network=msg.get("network"),
                    )

            elif msg_type == "tool_result":
                # Future: route result back to pending action
                request_id = msg.get("request_id", "")
                result = msg.get("result", "")
                logger.info("[WS-DEVICE] Tool result from %s (req %s): %s",
                            device_id, request_id, str(result)[:100])

            elif msg_type == "unregister":
                break

            else:
                await websocket.send_json({"type": "error", "detail": f"Unknown type: {msg_type}"})

    except WebSocketDisconnect:
        logger.info("[WS-DEVICE] %s disconnected", device_id)
    except Exception as e:
        logger.error("[WS-DEVICE] %s error: %s", device_id, e)
    finally:
        if device_id in _active_device_websockets:
            del _active_device_websockets[device_id]
        device_manager.unregister(device_id)
        logger.info("[WS-DEVICE] %s cleaned up", device_id)


# =============================================================================
# DEVELOPER CONSOLE REST APIs
# =============================================================================
# These endpoints power the Developer Console (debug dashboard).
# They expose internal state: events, devices, tools, tasks, cache, context.

@app.get("/api/devices")
async def api_devices():
    """List all connected devices."""
    return {"devices": device_manager.to_api_list(), "count": device_manager.get_device_count()}


@app.get("/api/devices/online")
async def api_devices_online():
    """List only online devices."""
    devices = device_manager.get_online_devices()
    return {"devices": [d.to_dict() for d in devices], "count": len(devices)}


@app.get("/api/tools")
async def api_tools():
    """List all registered tools with full schema."""
    from app.services.tools.tool_schema import tool_registry
    return {"tools": tool_registry.to_api_list(), "count": len(tool_registry.all_tools())}


@app.get("/api/events")
async def api_events(limit: int = 50, type: str = None):
    """Get recent events from the event bus."""
    from app.services.event_bus import event_bus
    events = event_bus.recent_events(limit=limit, event_type=type)
    return {"events": events, "stats": event_bus.stats()}


@app.get("/api/cache")
async def api_cache():
    """Get tool cache statistics."""
    from app.services.tools.tool_cache import tool_cache
    return tool_cache.stats()


@app.post("/api/cache/clear")
async def api_cache_clear():
    """Clear the tool cache."""
    from app.services.tools.tool_cache import tool_cache
    cleared = tool_cache.clear()
    return {"status": "ok", "cleared": cleared}


@app.get("/api/tasks")
async def api_tasks(limit: int = 50):
    """List recent tasks from the action manager."""
    from app.services.action_manager import action_manager
    return {"tasks": action_manager.to_api_list(limit=limit)}


@app.get("/api/tasks/active")
async def api_tasks_active():
    """List currently active (pending/running) tasks."""
    from app.services.action_manager import action_manager
    tasks = action_manager.get_active_tasks()
    return {"tasks": [t.to_dict() for t in tasks], "count": len(tasks)}


@app.post("/api/tasks/cancel/{task_id}")
async def api_task_cancel(task_id: str):
    """Cancel a running task."""
    from app.services.action_manager import action_manager
    ok = action_manager.cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"status": "cancelled", "task_id": task_id}


@app.get("/api/context")
async def api_context():
    """Get the current context snapshot."""
    from app.services.context_engine import context_engine
    ctx = context_engine.build_context()
    return ctx


@app.get("/api/platform/status")
async def api_platform_status():
    """Full platform status overview."""
    from app.services.tools.tool_schema import tool_registry
    from app.services.tools.tool_cache import tool_cache
    from app.services.event_bus import event_bus
    from app.services.action_manager import action_manager
    from app.services.context_engine import context_engine

    return {
        "platform": "JARVIS Phase 0",
        "version": "0.1.0",
        "services": {
            "vector_store": vector_store_service is not None,
            "groq_general": groq_service is not None,
            "groq_realtime": realtime_service is not None,
            "brain": brain_service is not None,
            "chat": chat_service is not None,
        },
        "tools": {
            "total": len(tool_registry.all_tools()),
            "local": len(tool_registry.local_tools()),
            "remote": len(tool_registry.remote_tools()),
            "custom": len(tool_registry.custom_tools()),
        },
        "cache": tool_cache.stats(),
        "events": event_bus.stats(),
        "devices": {
            "total": device_manager.get_device_count(),
            "online": len(device_manager.get_online_devices()),
        },
        "tasks": {
            "active": len(action_manager.get_active_tasks()),
        },
    }


# ==============================================================================
# CUSTOM ACTION ENGINE APIs
# ==============================================================================

@app.get("/api/custom-actions")
async def api_list_custom_actions():
    """List all custom actions for the current user."""
    from app.services.tools.custom_action_manager import custom_action_manager
    from config import CURRENT_USER_ID
    actions = custom_action_manager.get_all_actions(CURRENT_USER_ID)
    return {"user_id": CURRENT_USER_ID, "count": len(actions), "actions": actions}


@app.delete("/api/custom-actions/{action_name}")
async def api_delete_custom_action(action_name: str):
    """Delete a custom action permanently."""
    from app.services.tools.custom_action_manager import custom_action_manager
    from app.services.tools.tool_schema import tool_registry
    from config import CURRENT_USER_ID

    success, msg = custom_action_manager.delete_action(CURRENT_USER_ID, action_name)
    if success:
        tool_registry.unregister(action_name)
        return {"success": True, "message": msg}
    else:
        raise HTTPException(status_code=404, detail=msg)


@app.post("/api/custom-actions/test")
async def api_test_custom_action(request: dict):
    """Dry-run a custom action without persisting it."""
    from app.services.tools.custom_action_manager import custom_action_manager
    action_name = request.get("name", "")
    params = request.get("params", [])
    if not action_name:
        raise HTTPException(status_code=400, detail="Action name is required.")

    action_def = custom_action_manager.get_action(action_name)
    if not action_def:
        raise HTTPException(status_code=404, detail=f"Action '{action_name}' not found.")

    result = custom_action_manager.execute_action(action_name, params)
    return {"action": action_name, "result": result}


_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/app", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/app/", status_code=302)

def run():
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )

if __name__ == "__main__":
    run()
    