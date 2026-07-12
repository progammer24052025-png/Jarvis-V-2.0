import logging
import re
import time
from typing import List, Optional, Tuple, Literal

from config import GROQ_API_KEYS, GROQ_BRAIN_MODEL

logger = logging.getLogger("J.A.R.V.I.S")

QueryType = Literal["general", "realtime"]
MAX_CONTEXT_TURNS = 6
MAX_MESSAGE_PREVIEW = 500
REASONING_GENERAL = "Answerable from knowledge and context"
REASONING_REALTIME = "Needs live web search"
REASONING_DEFAULT = "Brain unavailable; defaulting to realtime"
REASONING_UNCLEAR = "Unclear; defaulting to realtime"
REASONING_REGEX = "Regex classifier (fast path)"

# ---------------------------------------------------------------------------
# Regex-based fast classifier
# ---------------------------------------------------------------------------
# Patterns that confidently map to "general" (no web search needed).
# If matched, returns in ~1-5ms instead of 200-500ms LLM call.
# Only matches when confidence is very high; ambiguous cases fall through
# to the LLM brain.

_REGEX_GENERAL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Greetings
    (re.compile(r"^(hi|hello|hey|howdy|good\s*(morning|afternoon|evening|night)|sup|yo|hola|namaste)\b", re.I), "greeting"),
    # Goodbye
    (re.compile(r"^(bye|goodbye|good\s*night|see\s*you|take\s*care|later|cya|jarvis\s*bye|exit\s*jarvis|good\s*bye)\b", re.I), "farewell"),
    # Thanks
    (re.compile(r"^(thanks|thank\s*you|thx|ty|appreciate\s*it|cheers)\b", re.I), "thanks"),
    # Time / date
    (re.compile(r"(what('?s|\s+is)\s+the\s+time|current\s+time|what\s+time\s+is\s+it|today'?s?\s+date|what\s+day\s+is\s+it|what'?s\s+the\s+date)", re.I), "time_date"),
    # Open app / launch
    (re.compile(r"^(open|launch|start|run)\s+\S+", re.I), "open_app"),
    # Close app
    (re.compile(r"^(close|quit|exit|kill|stop|end)\s+\S+", re.I), "close_app"),
    # System controls
    (re.compile(r"^(lock|shutdown|restart|sleep|mute|unmute|volume\s+(up|down|\d)|brightness\s+(up|down|\d)|increase\s+(volume|brightness)|decrease\s+(volume|brightness))", re.I), "system_control"),
    # Media playback
    (re.compile(r"^(play|pause|skip|next\s+track|previous\s+track|resume)\b", re.I), "media_play"),
    # YouTube / Spotify
    (re.compile(r"(play\s+.+\s+on\s+(youtube|spotify)|search\s+(youtube|spotify)\s+for|on\s+youtube)", re.I), "media_platform"),
    # Screenshot
    (re.compile(r"(take\s+a?\s*screenshot|screenshot|screen\s*grab|capture\s*(the\s+)?screen)", re.I), "screenshot"),
    # File operations
    (re.compile(r"^(create|write|save|make)\s+(a\s+)?(file|note|document|text)", re.I), "file_write"),
    (re.compile(r"^(read|open|show|view)\s+(the\s+)?(file|document)", re.I), "file_read"),
    (re.compile(r"^(delete|remove)\s+(the\s+)?(file|folder)", re.I), "file_delete"),
    # Git commands
    (re.compile(r"(git\s+(status|log|diff|push|pull|commit|branch|merge|checkout))", re.I), "git_command"),
    # Reminder
    (re.compile(r"(set\s+(a\s+)?reminder|remind\s+me|set\s+(a\s+)?alarm|set\s+(a\s+)?timer)", re.I), "reminder"),
    # System info / PC health
    (re.compile(r"(system\s*info|pc\s*health|cpu\s*usage|ram\s*usage|disk\s*space|battery\s*(status|level|percentage)|network\s*speed|wifi\s*(info|status|name))", re.I), "system_info"),
    # Desktop / folder listing
    (re.compile(r"(what('?s|\s+is)\s+(on|in)\s+(my\s+)?(desktop|folder)|list\s+(the\s+)?(desktop|folder)|show\s+(my\s+)?desktop)", re.I), "list_files"),
    # Installed apps
    (re.compile(r"(list\s+(installed|all)\s+apps|what\s+apps\s+(are|do)\s+(installed|do\s+i\s+have))", re.I), "installed_apps"),
    # Keyboard shortcuts
    (re.compile(r"(alt\s*tab|switch\s+(window|tab|app)|minimize|maximize|fullscreen)", re.I), "keyboard_nav"),
    # Browser control
    (re.compile(r"(new\s+tab|close\s+tab|next\s+tab|previous\s+tab|refresh\s+(the\s+)?page|browser\s+(tab|control))", re.I), "browser_control"),
    # Generate image
    (re.compile(r"(generate\s+(an?\s+)?image|create\s+(an?\s+)?image|make\s+(an?\s+)?image|ai\s+image|draw\s+|paint\s+)", re.I), "image_gen"),
    # Weather (local tool)
    (re.compile(r"(weather|temperature|forecast|how'?s?\s+the\s+weather)", re.I), "weather"),
    # Jokes / casual
    (re.compile(r"(tell\s+(me\s+)?(a\s+)?joke|make\s+me\s+laugh|say\s+something\s+funny)", re.I), "joke"),
    # Identity questions
    (re.compile(r"(who\s+are\s+you|what\s+are\s+you|your\s+name|are\s+you\s+(a\s+)?(ai|bot|jarvis)|is\s+your\s+name)", re.I), "identity"),
    # Yes / No / confirmation
    (re.compile(r"^(yes|yeah|yep|sure|ok|okay|confirm|no|nope|nah|cancel)\b", re.I), "confirmation"),
    # How are you
    (re.compile(r"(how\s+are\s+you|how\s+do\s+you\s+feel|are\s+you\s+(ok|okay|good|well|tired))", re.I), "how_are_you"),
    # Create folder
    (re.compile(r"(create\s+(a\s+)?folder|make\s+(a\s+)?folder|new\s+folder)", re.I), "folder_create"),
    # Move / rename
    (re.compile(r"(move\s+.+\s+to|rename\s+.+\s+to)", re.I), "file_move_rename"),
    # Open URL
    (re.compile(r"(open\s+(url|website|site|link|page)|go\s+to\s+https?://|navigate\s+to)", re.I), "open_url"),
]


def _regex_classify(user_message: str) -> Optional[QueryType]:
    """
    Attempt to classify the message using regex patterns only.
    Returns "general" if a pattern matches confidently, None if uncertain.
    Never returns "realtime" -- if regex can't determine it, LLM handles it.

    Target: 1-5ms vs 200-500ms LLM call.
    """
    msg = user_message.strip()
    if not msg or len(msg) < 2:
        return None  # too short to classify

    for pattern, _category in _REGEX_GENERAL_PATTERNS:
        if pattern.search(msg):
            logger.debug("[BRAIN-REGEX] Matched pattern '%s' -> general", _category)
            return "general"

    return None  # uncertain -> fall through to LLM

_BRAIN_SYSTEM_PROMPT = """You are a query classifier for an AI assistant. Your ONLY job is to decide whether a user's message needs LIVE WEB SEARCH or not.

Output EXACTLY one word: either "general" or "realtime".

- general: ONLY questions that are purely from static knowledge, learning data, or conversation. Examples: "Tell me a joke", "What did I ask you before?", "Open YouTube", "Write a poem about cats", "How do I improve my coding?", "What is the capital of France?", casual chit-chat. NO questions about people, current events, or things that could change.

- realtime: ALWAYS use realtime for:
  * ANY question about a person (famous or not): "Who is Elon Musk?", "Tell me about [person]", "What is [name] known for?", "Who is that actor?" — the LLM has no real-time data; web search finds current info and may find info on lesser-known people.
  * Anything that could have changed: news, weather, stock prices, sports scores, elections, "latest", "current", "today", "recent", "now".
  * Factual lookups where real-time data would be better: events, companies, products, releases, versions.

STRONG RULE: If the question is about a person (who, what, tell me about, etc.) → ALWAYS "realtime". The LLM cannot know current facts; web search can.

When in doubt, prefer "realtime" — it's better to search when not needed than to miss current information.

Output ONLY the word. No explanation, no punctuation, no other text."""

class BrainService:
    def __init__(self):
        self._llms = []
        self._regex_enabled = True
        if GROQ_API_KEYS:
            try:
                from langchain_groq import ChatGroq
                self._llms = [
                    ChatGroq(
                        groq_api_key=key,
                        model_name=GROQ_BRAIN_MODEL,
                        temperature=0.0,
                        max_tokens=20,
                        request_timeout=10,
                    )
                    for key in GROQ_API_KEYS
                ]
                logger.info("[BRAIN] Groq brain initialized (model: %s) with %d key(s)", GROQ_BRAIN_MODEL, len(self._llms))
            except Exception as e:
                logger.warning("[BRAIN] Failed to create Groq brain: %s", e)
        if not self._llms:
            logger.warning("[BRAIN] No API keys. Classification will default to realtime.")

    def classify(
        self,
        user_message: str,
        chat_history: Optional[List[Tuple[str, str]]] = None,
        key_index: int = 0,
    ) -> Tuple[QueryType, str, int]:
        # --- FAST PATH: Regex classifier (1-5ms) ---
        if self._regex_enabled:
            t_regex = time.perf_counter()
            regex_result = _regex_classify(user_message)
            regex_ms = int((time.perf_counter() - t_regex) * 1000)
            if regex_result is not None:
                logger.info("[BRAIN] Regex classified '%s' as %s in %d ms",
                            user_message[:60], regex_result, regex_ms)
                return (regex_result, REASONING_REGEX, regex_ms)
            logger.debug("[BRAIN] Regex uncertain in %d ms, falling through to LLM", regex_ms)

        # --- SLOW PATH: LLM classifier (200-500ms) ---
        if not self._llms:
            return ("realtime", REASONING_DEFAULT, 0)
        context_lines = []
        if chat_history:
            for u, a in chat_history[-MAX_CONTEXT_TURNS:]:
                u_preview = (u or "")[:MAX_MESSAGE_PREVIEW] + ("…" if len(u or "") > MAX_MESSAGE_PREVIEW else "")
                a_preview = (a or "")[:MAX_MESSAGE_PREVIEW] + ("…" if len(a or "") > MAX_MESSAGE_PREVIEW else "")
                context_lines.append(f"User: {u_preview}")
                context_lines.append(f"Assistant: {a_preview}")

        context_block = "\n".join(context_lines) if context_lines else "(No prior conversation)"
        msg_preview = (user_message or "")[:MAX_MESSAGE_PREVIEW]
        user_content = f"""Conversation so far:
{context_block}

Current user message: {msg_preview}

        Classify the current message. Output ONLY: general or realtime"""

        t0 = time.perf_counter()
        try:
            from langchain_core.messages import SystemMessage, HumanMessage
            idx = key_index % len(self._llms)
            llm = self._llms[idx]
            response = llm.invoke([
                SystemMessage(content=_BRAIN_SYSTEM_PROMPT),
                HumanMessage(content=user_content),
            ])
            text = (response.content or "").strip().lower()
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.warning("[BRAIN] Groq error after %d ms. Defaulting to realtime.", elapsed_ms, e)
            return ("realtime", f"API error: {str(e)[:60]}", elapsed_ms)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if re.search(r"\brealtime\b", text):
            logger.info("[BRAIN] Groq (key #%d) returned realtime in %d ms", key_index + 1, elapsed_ms)
            return ("realtime", REASONING_REALTIME, elapsed_ms)
        if re.search(r"\bgeneral\b", text):
            logger.info("[BRAIN] Groq (key #%d) returned general in %d ms", key_index + 1, elapsed_ms)
            return ("general", REASONING_GENERAL, elapsed_ms)
        logger.warning("[BRAIN] Unexpected output: %r in %d ms. Defaulting to realtime.", text[:100], elapsed_ms)
        return ("realtime", REASONING_UNCLEAR, elapsed_ms)
    