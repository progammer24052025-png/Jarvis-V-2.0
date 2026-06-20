from pathlib import Path
from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import edge_tts
from pydantic import BaseModel
from app.models import ChatRequest, ChatResponse, TTSRequest, SettingsUpdate
import config as config_module
from app.services.tools.tool_executor import process_text_for_actions

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
    VECTOR_STORE_DIR, GROQ_API_KEYS, GROQ_MODEL, TAVILY_API_KEY,
    EMBEDDING_MODEL, CHUNK_SIZE, CHUNK_OVERLAP, MAX_CHAT_HISTORY_TURNS,
    ASSISTANT_NAME, TTS_VOICE, TTS_RATE, CORS_ORIGINS,
)

# ── Simple in-memory rate limiter (no external dependency) ──
_rate_limit_store: dict = defaultdict(list)  # ip -> [timestamps]
RATE_LIMIT_MAX = 30     # max requests per window
RATE_LIMIT_WINDOW = 60  # window in seconds

def _check_rate_limit(request: Request) -> bool:
    """Return True if the request should be rate-limited (denied)."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    timestamps = _rate_limit_store[client_ip]
    # Prune old entries
    _rate_limit_store[client_ip] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_MAX:
        return True
    _rate_limit_store[client_ip].append(now)
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
                        reminder["remind_at"] = (remind_dt + timedelta(days=30)).isoformat()
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("[REMINDER] Check error: %s", e)


from datetime import timedelta


def search_vector_store_for_files(query: str, top_k: int = 3) -> list:
    """Search the vector store for relevant file content."""
    if not vector_store_service or not vector_store_service.vector_store:
        return []
    
    try:
        retriever = vector_store_service.vector_store.as_retriever(
            search_type="similarity",
            search_kwargs={"k": top_k}
        )
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

def print_title():
    """Print the J.A.R.V.I.S ASCII art title."""
    title = """
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
    print(title)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global vector_store_service, groq_service, realtime_service, brain_service, chat_service, _reminder_task

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
        chat_service = ChatService(groq_service, realtime_service, brain_service)
        logger.info("Chat service initialized successfully")

        # Start reminder background task
        logger.info("Starting reminder monitoring service...")
        _reminder_task = asyncio.create_task(_run_reminder_checker())
        logger.info("Reminder service started")

        logger.info("=" * 60)
        logger.info("Service Status:")
        logger.info("  - Vector Store: Ready")
        logger.info("  - Groq AI (General): Ready")
        logger.info("  - Groq AI (Realtime): Ready")
        logger.info("  - Brain (Groq): Ready")
        logger.info("  - Chat Service: Ready")
        logger.info("  - Reminder Service: Ready")
        logger.info("=" * 60)
        logger.info("J.A.R.V.I.S is online and ready!")
        logger.info("API: http://localhost:8000")
        logger.info("Frontend: http://localhost:8000/app/ (open in browser)")
        logger.info("=" * 60)

        yield

        # Cancel reminder task on shutdown
        if _reminder_task:
            _reminder_task.cancel()
            try:
                await _reminder_task
            except asyncio.CancelledError:
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
        text = re.sub(r'\[ACTION:\w+\([^)]*\)\]', '', text).strip()
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
    try:
        cleaned_text, action_results = process_text_for_actions(full_text)
        for ar in action_results:
            yield f"data: {json.dumps({'activity': {'event': 'tool_executed', 'tool': ar['tool'], 'result': ar['result']}})}\n\n"
            logger.info("[TOOL] Executed %s -> %s", ar['tool'], str(ar['result'])[:100])
    except Exception as e:
        logger.warning("[TOOL] Action processing error: %s", e)

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

        # ── Inject uploaded file context if available ──
        # Keep file context (don't pop) so it persists for future messages
        user_msg = request.message
        file_ctx = uploaded_file_context.get(session_id) or uploaded_file_context.get("global")
        
        # Also search vector store for relevant file content
        vector_file_results = search_vector_store_for_files(request.message, top_k=2)
        
        if file_ctx or vector_file_results:
            context_parts = []
            
            # Add explicitly uploaded file context
            if file_ctx:
                context_parts.append(
                    f"[The user previously uploaded a file: {file_ctx['filename']} ({file_ctx['file_type']}, {file_ctx['words']} words)]\n"
                    f"--- FILE CONTENT ---\n{file_ctx['content']}\n--- END FILE CONTENT ---"
                )
            
            # Add vector store search results for uploaded files
            if vector_file_results:
                for result in vector_file_results:
                    context_parts.append(
                        f"[Relevant content from uploaded file: {result['source']}]\n"
                        f"--- CONTENT ---\n{result['content'][:2000]}\n--- END CONTENT ---"
                    )
            
            user_msg = (
                f"{chr(10).join(context_parts)}\n\n"
                f"User's question: {request.message}"
            )
            logger.info("[UPLOAD-CONTEXT] Injected file context + vector search results into session %s", session_id[:12])
            f"[The user uploaded a file: {file_ctx['filename']} ({file_ctx['file_type']}, {file_ctx['words']} words)]\n"
            f"--- FILE CONTENT START ---\n{file_ctx['content']}\n--- FILE CONTENT END ---\n\n"
            f"User's question: {request.message}"
            
            logger.info("[UPLOAD-CONTEXT] Injected %s (%d chars) into session %s",
                        file_ctx['filename'], len(file_ctx['content']), session_id[:12])

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
        user_msg = request.message
        # Keep file context (don't pop) so it persists for future messages
        file_ctx = uploaded_file_context.get(session_id) or uploaded_file_context.get("global")
        
        # Also search vector store for relevant file content
        vector_file_results = search_vector_store_for_files(request.message, top_k=2)
        
        if file_ctx or vector_file_results:
            context_parts = []
            
            # Add explicitly uploaded file context
            if file_ctx:
                context_parts.append(
                    f"[The user previously uploaded a file: {file_ctx['filename']} ({file_ctx['file_type']}, {file_ctx['words']} words)]\n"
                    f"--- FILE CONTENT ---\n{file_ctx['content']}\n--- END FILE CONTENT ---"
                )
            
            # Add vector store search results for uploaded files
            if vector_file_results:
                for result in vector_file_results:
                    context_parts.append(
                        f"[Relevant content from uploaded file: {result['source']}]\n"
                        f"--- CONTENT ---\n{result['content'][:2000]}\n--- END CONTENT ---"
                    )
            
            user_msg = (
                f"{chr(10).join(context_parts)}\n\n"
                f"User's question: {request.message}"
            )
            logger.info("[UPLOAD-CONTEXT] Injected file context + vector search results into session %s", session_id[:12])
        
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
        user_msg = request.message
        # Keep file context (don't pop) so it persists for future messages
        file_ctx = uploaded_file_context.get(session_id) or uploaded_file_context.get("global")
        
        # Also search vector store for relevant file content
        vector_file_results = search_vector_store_for_files(request.message, top_k=2)
        
        if file_ctx or vector_file_results:
            context_parts = []
            
            # Add explicitly uploaded file context
            if file_ctx:
                context_parts.append(
                    f"[The user previously uploaded a file: {file_ctx['filename']} ({file_ctx['file_type']}, {file_ctx['words']} words)]\n"
                    f"--- FILE CONTENT ---\n{file_ctx['content']}\n--- END FILE CONTENT ---"
                )
            
            # Add vector store search results for uploaded files
            if vector_file_results:
                for result in vector_file_results:
                    context_parts.append(
                        f"[Relevant content from uploaded file: {result['source']}]\n"
                        f"--- CONTENT ---\n{result['content'][:2000]}\n--- END CONTENT ---"
                    )
            
            user_msg = (
                f"{chr(10).join(context_parts)}\n\n"
                f"User's question: {request.message}"
            )
            logger.info("[UPLOAD-CONTEXT] Injected file context + vector search results into session %s", session_id[:12])
        
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
        for fp in sorted(config_module.CHATS_DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
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
        import datetime
        uploaded_files_db[safe_name] = {
            "filename": safe_name,
            "original_name": filename,
            "content": text,  # Store full content, not truncated
            "file_type": file_type,
            "words": word_count,
            "uploaded_at": datetime.datetime.now().isoformat(),
            "session_id": session_id,
        }
        logger.info("[UPLOAD] Added to persistent file database: %s", safe_name)

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
        summary_response = await groq_service.client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that creates concise summaries of documents. Provide a clear, well-structured summary with key points."},
                {"role": "user", "content": f"Please summarize the following document:\n\n{content}"}
            ],
            temperature=0.3,
            max_tokens=1024,
        )
        summary = summary_response.choices[0].message.content
        
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

import uuid
from datetime import datetime, timedelta

def parse_datetime(dt_str: str) -> datetime:
    """Parse datetime string with flexible formats."""
    # Handle datetime-local format (YYYY-MM-DDTHH:MM)
    if 'T' not in dt_str and ' ' not in dt_str:
        dt_str = dt_str.replace('-', 'T')
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
        # Use AI to parse the reminder request
        response = await groq_service.client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": """You are a reminder parser. Extract the reminder details from the user's message.
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
Only return valid JSON, no other text."""},
                {"role": "user", "content": body.message}
            ],
            temperature=0.1,
            max_tokens=256,
        )
        
        import json
        result = json.loads(response.choices[0].message.content)
        
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
        
        logger.info("[REMINDER] AI created: %s at %s", reminder_id, remind_dt)
        return {"status": "ok", "reminder": reminder, "parsed": result}
        
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Could not parse AI response. Please try again.")
    except Exception as e:
        logger.error("[REMINDER] AI creation failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to create reminder: {str(e)}")


# ── Webhook Endpoints ──

class WebhookCreate(BaseModel):
    url: str
    events: list[str] = ["chat_completed"]  # chat_completed, reminder_triggered, file_uploaded
    active: bool = True

@app.post("/webhooks")
async def create_webhook(body: WebhookCreate):
    """Create a new webhook for notifications."""
    import uuid
    webhook_id = str(uuid.uuid4())[:8]
    webhook = {
        "id": webhook_id,
        "url": body.url,
        "events": body.events,
        "active": body.active,
        "created_at": datetime.now().isoformat(),
    }
    webhooks_db[webhook_id] = webhook
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
    return {"status": "ok", "message": "Calendar configuration updated"}

@app.get("/integrations/calendar/auth")
async def calendar_auth():
    """Start Google OAuth flow."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=400, detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID environment variable.")
    
    import urllib.parse
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
        from datetime import datetime, timedelta
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
                    return await list_calendar_events(start_date, end_date)
            except:
                calendar_config["token_json"] = ""
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
    