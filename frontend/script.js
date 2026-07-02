/* ================================================================
   J.A.R.V.I.S Frontend — Main Application Logic
   ================================================================

   ARCHITECTURE OVERVIEW
   ---------------------
   This file powers the entire frontend of the J.A.R.V.I.S AI assistant.
   It handles:

   1. CHAT MESSAGING — The user types (or speaks) a message, which is
      sent to the backend via a POST request. The backend responds using
      Server-Sent Events (SSE), allowing the reply to stream in
      token-by-token (like ChatGPT's typing effect).

   2. TEXT-TO-SPEECH (TTS) — When TTS is enabled, the backend also
      sends base64-encoded audio chunks inside the SSE stream. These
      are queued up and played sequentially through a single <audio>
      element. This queue-based approach prevents overlapping audio
      and supports mobile browsers (especially iOS/Safari).

   3. SPEECH RECOGNITION — The Web Speech API captures the user's
      voice, transcribes it in real time, and auto-sends the final
      transcript as a chat message.

   4. ANIMATED ORB — A WebGL-powered visual orb (rendered by a
      separate OrbRenderer class) acts as a visual indicator. It
      "activates" when J.A.R.V.I.S is speaking and goes idle otherwise.

   5. MODE SWITCHING — The UI supports two modes:
      - "General" mode  → uses the /chat/stream endpoint
      - "Realtime" mode → uses the /chat/realtime/stream endpoint
      The mode determines which backend pipeline processes the message.

   6. SESSION MANAGEMENT — A session ID is returned by the server on
      the first message. Subsequent messages include that ID so the
      backend can maintain conversation context. Starting a "New Chat"
      clears the session.

   DATA FLOW (simplified):
   User input → sendMessage() → POST to backend → SSE stream opens →
   tokens arrive as JSON chunks → rendered into the DOM in real time →
   optional audio chunks are enqueued in TTSPlayer → played sequentially.

   ================================================================ */

/*
 * API — The base URL for all backend requests.
 *
 * In production, this resolves to the same origin the page was loaded from
 * (e.g., "https://jarvis.example.com"). During local development, it falls
 * back to "http://localhost:8000" (the default FastAPI dev server port).
 *
 * `window.location.origin` gives us the protocol + host + port of the
 * current page, making the frontend deployment-agnostic (no hardcoded URLs).
 */
const API = (typeof window !== 'undefined' && window.location.origin)
    ? window.location.origin
    : 'http://localhost:8000';

/* ================================================================
   APPLICATION STATE
   ================================================================
   These variables track the global state of the application. They are
   intentionally kept as simple top-level variables rather than in a
   class or store, since this is a single-page app with one chat view.
   ================================================================ */

/*
 * sessionId — Unique conversation identifier returned by the server.
 * Starts as null (no conversation yet). Once the first server response
 * arrives, it contains a UUID string that we send back with every
 * subsequent message so the backend knows which conversation we're in.
 */
let sessionId = null;

/*
 * currentMode — Which AI pipeline to use: 'jarvis', 'general', or 'realtime'.
 * - jarvis:   Unified route; brain classifies, then routes to general or realtime.
 * - general:  Direct /chat/stream (no web search).
 * - realtime: Direct /chat/realtime/stream (with Tavily web search).
 */
let currentMode = 'jarvis';

/*
 * isStreaming — Guard flag that is true while an SSE response is being
 * received. Prevents the user from sending another message while the
 * assistant is still replying (avoids race conditions and garbled output).
 */
let isStreaming = false;

/*
 * isListening — True while the speech recognition engine is actively
 * capturing audio from the microphone. Used to toggle the mic button
 * styling and to decide whether to start or stop listening on click.
 */
let isListening = false;

/*
 * autoListenMode — When true, mic stays "on": after each voice-sent message,
 * we stop listening during the AI response, then auto-restart when the AI
 * and TTS playback are complete. User clicks mic again to turn off.
 */
let autoListenMode = false;

/* Speech recognition config */
const SPEECH_ERROR_MAX_RETRIES = 3;
let speechErrorRetryCount = 0;
const SPEECH_SEND_DELAY_MS = 500;   /* Pause after final transcript before sending (lets user add more) */
const SPEECH_RESTART_DELAY_MS = 700; /* Delay before restarting mic after AI+TTS complete (avoids echo) */
let speechSendTimeout = null;
let pendingSendTranscript = null;
let safariVoiceHintShown = false;

/* Live interim transcription via Web Speech API (shows words as you speak) */
let _interimRecognition = null;   /* SpeechRecognition instance for live preview */
let _interimFinalText = '';       /* Final transcript from Web Speech API (before Whisper replaces it) */

/*
 * orb — Reference to the OrbRenderer instance (the animated WebGL orb).
 * Null if OrbRenderer is unavailable or failed to initialize.
 * We call orb.setActive(true/false) to animate it during TTS playback.
 */
let orb = null;

/*
 * recognition — The SpeechRecognition instance from the Web Speech API.
 * Null if the browser doesn't support speech recognition.
 */
let recognition = null;

/*
 * ttsPlayer — Instance of the TTSPlayer class (defined below) that
 * manages queuing and playing audio segments received from the server.
 */
let ttsPlayer = null;

/*
 * settings — User preferences (auto-open panels). Stored in localStorage.
 */
const SETTINGS_KEY = 'jarvis_settings';
const DEFAULT_SETTINGS = { autoOpenActivity: true, autoOpenSearchResults: true, thinkingSounds: true };

/* Pre-starter: pre-generated MP3 file names (from app.generate_thinking_audio) */
const PRE_STARTER_FILES = ['starter_1', 'starter_2', 'starter_3', 'starter_4', 'starter_5', 'starter_6', 'starter_7', 'starter_8', 'starter_9', 'starter_10'];
/* Pre-loaded base64 audio cache — populated at init for instant playback */
let PRE_STARTER_CACHE = {};
let settings = { ...DEFAULT_SETTINGS };

/* ================================================================
   DOM REFERENCES
   ================================================================
   We grab references to frequently-used DOM elements once at startup
   rather than querying for them every time we need them. This is both
   a performance optimization and a readability convenience.
   ================================================================ */

/*
 * $ — Shorthand helper for document.getElementById. Writing $('foo')
 * is more concise than document.getElementById('foo').
 */
const $ = id => document.getElementById(id);

const chatMessages = $('chat-messages');   // The scrollable container that holds all chat messages
const messageInput = $('message-input');   // The <textarea> where the user types their message
const sendBtn      = $('send-btn');        // The send button (arrow icon)
const micBtn       = $('mic-btn');         // The microphone button for speech-to-text
const ttsBtn       = $('tts-btn');         // The speaker button to toggle text-to-speech
const newChatBtn   = $('new-chat-btn');    // The "New Chat" button that resets the conversation
const charCount    = $('char-count');      // Shows character count when the message gets long
const welcomeTitle = $('welcome-title');   // The greeting text on the welcome screen ("Good morning.", etc.)
const modeSlider   = $('mode-slider');     // The sliding pill indicator behind the mode toggle buttons
const btnJarvis    = $('btn-jarvis');      // The "Jarvis" mode button (unified, brain-routed)
const btnGeneral   = $('btn-general');     // The "General" mode button
const btnRealtime  = $('btn-realtime');    // The "Realtime" mode button
const statusDot    = document.querySelector('.status-dot');  // Green/red dot showing backend status
const statusText   = document.querySelector('.status-text'); // Text next to the dot ("Online" / "Offline")
const orbContainer = $('orb-container');   // The container <div> that holds the WebGL orb canvas
const searchResultsToggle = $('search-results-toggle');   // Header button to open search results panel
const searchResultsWidget = $('search-results-widget');   // Right-side panel for Tavily search data
const searchResultsClose  = $('search-results-close');    // Close button inside the panel
const searchResultsQuery  = $('search-results-query');    // Displays the search query
const searchResultsAnswer = $('search-results-answer');   // Displays the AI answer from search
const searchResultsList   = $('search-results-list');     // Container for source result cards
const activityPanel       = $('activity-panel');          // Left panel for Jarvis activity flow
const activityToggle      = $('activity-toggle');          // Header button to open activity panel
const activityClose       = $('activity-close');           // Close button inside activity panel
const activityList        = $('activity-list');            // Container for activity items
const panelOverlay        = $('panel-overlay');            // Backdrop when a side panel is open
const speechWidget        = $('speech-widget');            // Live speech-to-text display
const speechWidgetText    = $('speech-widget-text');       // Transcript text element
const settingsBtn         = $('settings-btn');              // Gear icon to open settings
const settingsPanel       = $('settings-panel');            // Settings modal/panel
const settingsClose       = $('settings-close');           // Close settings
const toggleAutoActivity  = $('toggle-auto-activity');     // Auto-open activity panel
const toggleAutoSearch    = $('toggle-auto-search');        // Auto-open search results
const toggleThinkingSounds = $('toggle-thinking-sounds');   // Thinking sound effects
const toastContainer     = $('toast-container');           // Toast container for error/status feedback
const settingUserTitle   = $('setting-user-title');         // Personalization: user title input
const settingTtsVoice    = $('setting-tts-voice');          // Personalization: TTS voice input
const settingTtsRate     = $('setting-tts-rate');           // Personalization: TTS speed input
const settingsSaveBtn    = $('settings-save-btn');          // Save personalization settings

/* ================================================================
   PRE-STARTER PLAYER (Dedicated — never interrupted by TTS reset)
   ================================================================
   Plays one random pre-generated clip ("Oh wait.", etc.) on its own
   audio element. Runs independently so ttsPlayer.reset() cannot stop it.
   Flow: pre-starter plays; main TTS plays when first real chunk arrives.
   ================================================================ */
class PreStarterPlayer {
    constructor() {
        this.audio = document.createElement('audio');
        this.audio.preload = 'auto';
    }

    play(onComplete) {
        const loaded = PRE_STARTER_FILES.filter(f => PRE_STARTER_CACHE[f]);
        if (loaded.length === 0) {
            if (onComplete) onComplete();
            return;
        }
        const file = loaded[Math.floor(Math.random() * loaded.length)];
        const base64 = PRE_STARTER_CACHE[file];
        if (!base64) {
            if (onComplete) onComplete();
            return;
        }
        this.audio.src = 'data:audio/mp3;base64,' + base64;
        this.audio.currentTime = 0;
        let fired = false;
        const done = () => {
            if (fired) return;
            fired = true;
            this.audio.onended = null;
            this.audio.onerror = null;
            if (onComplete) onComplete();
        };
        this.audio.onended = done;
        this.audio.onerror = done;
        const p = this.audio.play();
        if (p) p.catch(done);
    }
}

let preStarterPlayer = null;

/* ================================================================
   TTS AUDIO PLAYER (Text-to-Speech Queue System)
   ================================================================

   HOW THE TTS QUEUE WORKS — EXPLAINED FOR LEARNERS
   -------------------------------------------------
   When TTS is enabled, the backend doesn't send one giant audio file.
   Instead, it sends many small base64-encoded MP3 *chunks* as part of
   the SSE stream (one chunk per sentence or phrase). This approach has
   two advantages:
     1. Audio starts playing before the full response is generated
        (lower latency — the user hears the first sentence immediately).
     2. Each chunk is small, so there's no long download wait.

   The TTSPlayer works like a conveyor belt:
     - enqueue() adds a new audio chunk to the end of the queue.
     - _playLoop() picks up chunks one by one and plays them.
     - When a chunk finishes playing (audio.onended), the loop moves
       to the next chunk.
     - When the queue is empty and no more chunks are arriving, playback
       stops and the orb goes back to idle.

   WHY A SINGLE <audio> ELEMENT?
   iOS Safari has strict autoplay policies — it only allows audio
   playback from a user-initiated event. By reusing one <audio> element
   that was "unlocked" during a user gesture, all subsequent plays
   through that same element are allowed. Creating new Audio() objects
   each time would trigger autoplay blocks on iOS.

   ================================================================ */
class TTSPlayer {
    /**
     * Creates a new TTSPlayer instance.
     *
     * Properties:
     *   queue    — Array of base64 audio strings waiting to be played.
     *   playing  — True if the play loop is currently running.
     *   enabled  — True if the user has toggled TTS on (via the speaker button).
     *   stopped  — True if playback was forcibly stopped (e.g., new chat).
     *              This prevents queued audio from playing after a stop.
     *   audio    — A single persistent <audio> element reused for all playback.
     */
    constructor() {
        this.queue = [];
        this.playing = false;
        this.enabled = true;   // TTS on by default
        this.stopped = false;
        this.audio = document.createElement('audio');
        this.audio.preload = 'auto';
    }

    /**
     * unlock() — "Warms up" the audio element so browsers (especially iOS
     * Safari) allow subsequent programmatic playback.
     *
     * This should be called during a user gesture (e.g., clicking "Send").
     *
     * It does two things:
     *   1. Plays a tiny silent WAV file on the <audio> element, which
     *      tells the browser "the user initiated audio playback."
     *   2. Creates a brief AudioContext oscillator at zero volume — this
     *      unlocks the Web Audio API context on iOS (a separate lock from
     *      the <audio> element).
     *
     * After this, the browser treats subsequent .play() calls on the same
     * <audio> element as user-initiated, even if they happen in an async
     * callback (like our SSE stream handler).
     */
    unlock() {
        // A minimal valid WAV file (44-byte header + 2 bytes of silence)
        const silentWav = 'data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA';
        this.audio.src = silentWav;
        const p = this.audio.play();
        if (p) p.catch(() => {});
        try {
            // Create a Web Audio context and play a zero-volume oscillator for <1ms
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const g = ctx.createGain();
            g.gain.value = 0;
            const o = ctx.createOscillator();
            o.connect(g);
            g.connect(ctx.destination);
            o.start(0);
            o.stop(ctx.currentTime + 0.001);
            setTimeout(() => ctx.close(), 200);
        } catch (_) {}
    }

    /**
     * enqueue(base64Audio) — Adds a base64-encoded MP3 chunk to the
     * playback queue.
     *
     * @param {string} base64Audio - The base64 string of the MP3 audio data.
     *
     * If TTS is disabled or playback has been force-stopped, the chunk
     * is silently discarded. Otherwise it's pushed onto the queue.
     * If the play loop isn't already running, we kick it off.
     */
    enqueue(base64Audio) {
        if (!this.enabled || this.stopped) return;
        this.queue.push(base64Audio);
        if (!this.playing) this._playLoop();
    }

    /**
     * stop() — Immediately halts all audio playback and clears the queue.
     *
     * Called when:
     *   - The user starts a "New Chat"
     *   - The user toggles TTS off while audio is playing
     *   - We need to reset before a new streaming response
     *
     * It also removes visual indicators (CSS classes on the TTS button,
     * the orb container, and deactivates the orb animation).
     */
    stop() {
        this.stopped = true;
        this.audio.pause();
        this.audio.removeAttribute('src');
        this.audio.load();                        // Fully resets the audio element
        this.queue = [];                           // Discard any pending audio chunks
        this.playing = false;
        if (ttsBtn) ttsBtn.classList.remove('tts-speaking');
        if (orbContainer) orbContainer.classList.remove('speaking');
        if (orb) orb.setActive(false);
        if (typeof this.onPlaybackComplete === 'function') this.onPlaybackComplete();   // AI stopped — maybe restart mic
    }

    /**
     * reset() — Stops playback AND clears the "stopped" flag so new
     * audio can be enqueued again.
     *
     * Called at the beginning of each new message send, or when the main
     * response arrives (to cut off thinking audio). Increments _loopId so
     * any in-flight _playLoop exits immediately.
     */
    reset() {
        this.stop();
        this.stopped = false;
        this._loopId = (this._loopId || 0) + 1;   // Supersede in-flight play loop
    }

    /**
     * _playLoop() — The internal playback engine. Processes the queue
     * one chunk at a time in a while-loop.
     *
     * WHY THE LOOP ID (_loopId)?
     * If stop() is called and then a new stream starts, there could be
     * two concurrent _playLoop() calls — the old one (still awaiting a
     * Promise) and the new one. The loop ID lets us detect when a loop
     * has been superseded: each invocation gets a unique ID, and if the
     * ID changes mid-loop (because a new loop started), the old loop
     * exits gracefully. This prevents double-playback or stale loops.
     *
     * VISUAL INDICATORS:
     * While playing, we add CSS classes 'tts-speaking' (to the button)
     * and 'speaking' (to the orb container) for visual feedback. These
     * are removed when the queue is drained or playback is stopped.
     */
    async _playLoop() {
        if (this.playing) return;
        this.playing = true;
        this._loopId = (this._loopId || 0) + 1;
        const myId = this._loopId;

        // Activate visual indicators: button glow + orb animation
        if (ttsBtn) ttsBtn.classList.add('tts-speaking');
        if (orbContainer) orbContainer.classList.add('speaking');
        if (orb) orb.setActive(true);

        // Process queued audio chunks one at a time
        while (this.queue.length > 0) {
            if (this.stopped || myId !== this._loopId) break;  // Exit if stopped or superseded
            const b64 = this.queue.shift();                     // Take the next chunk from the front
            try {
                await this._playB64(b64);                       // Wait for it to finish playing
            } catch (e) {
                console.warn('TTS segment error:', e);
            }
        }

        // If another loop took over, don't touch the shared state
        if (myId !== this._loopId) {
            this.playing = false;   // Allow new loop to start
            return;
        }
        this.playing = false;
        // Deactivate visual indicators
        if (ttsBtn) ttsBtn.classList.remove('tts-speaking');
        if (orbContainer) orbContainer.classList.remove('speaking');
        if (orb) orb.setActive(false);
        // Notify when playback is fully complete (for auto-restart listening)
        if (typeof this.onPlaybackComplete === 'function') this.onPlaybackComplete();
    }

    /**
     * _playB64(b64) — Plays a single base64-encoded MP3 chunk.
     *
     * @param {string} b64 - Base64-encoded MP3 audio data.
     * @returns {Promise<void>} Resolves when the audio finishes playing
     *                          (or errors out).
     *
     * Sets the <audio> element's src to a data URL and calls .play().
     * Returns a Promise that resolves on 'ended' or 'error', so the
     * _playLoop() can await it and move to the next chunk.
     */
    _playB64(b64) {
        return new Promise(resolve => {
            this.audio.src = 'data:audio/mp3;base64,' + b64;
            const done = () => { resolve(); };
            this.audio.onended = done;   // Normal completion
            this.audio.onerror = done;   // Error — resolve anyway so the loop continues
            const p = this.audio.play();
            if (p) p.catch(done);        // Handle play() rejection (e.g., autoplay block)
        });
    }
}

/* ================================================================
   INITIALIZATION
   ================================================================
   init() is the entry point for the entire application. It is called
   once when the DOM is fully loaded (see the DOMContentLoaded listener
   at the bottom of this file).

   It sets up every subsystem in the correct order:
     1. TTSPlayer — so audio is ready before any messages
     2. Greeting  — display a time-appropriate welcome message
     3. Orb       — initialize the WebGL visual
     4. Speech    — set up the microphone / speech recognition
     5. Health    — ping the backend to check if it's online
     6. Events    — wire up all button clicks and keyboard shortcuts
     7. Input     — auto-resize the textarea to fit content
   ================================================================ */
function init() {
    if (!chatMessages || !messageInput) {
        console.error('[JARVIS] Required DOM elements (chat-messages, message-input) not found.');
        return;
    }
    loadSettings();
    ttsPlayer = new TTSPlayer();
    ttsPlayer.onPlaybackComplete = maybeRestartListening;   // Auto-restart mic when TTS finishes
    if (ttsBtn) ttsBtn.classList.add('tts-active');   // Show TTS as on by default
    setGreeting();
    initOrb();
    initSpeech();
    preloadStarterAudio();   // Pre-load MP3s for instant playback
    preStarterPlayer = new PreStarterPlayer();   // Dedicated player for pre-starter (immune to ttsPlayer.reset)
    checkHealth();
    bindEvents();
    setMode(currentMode);   // Sync mode slider, labels, and activity toggle
    autoResizeInput();
}

/**
 * preloadStarterAudio() — Fetches pre-generated starter MP3s and caches as base64.
 * Enables instant playback when user sends (no network delay).
 */
async function preloadStarterAudio() {
    const base = (typeof window !== 'undefined' && window.location.origin) ? window.location.origin : '';
    for (const file of PRE_STARTER_FILES) {
        try {
            const r = await fetch(`${base}/app/audio/${file}.mp3`);
            if (!r.ok) continue;
            const blob = await r.blob();
            const base64 = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve((reader.result || '').split(',')[1] || '');
                reader.onerror = reject;
                reader.readAsDataURL(blob);
            });
            if (base64) PRE_STARTER_CACHE[file] = base64;
        } catch (_) {}
    }
}

function loadSettings() {
    try {
        const s = localStorage.getItem(SETTINGS_KEY);
        if (s) {
            const parsed = JSON.parse(s);
            settings = { ...DEFAULT_SETTINGS, ...parsed };
        }
        if (toggleAutoActivity) toggleAutoActivity.checked = settings.autoOpenActivity;
        if (toggleAutoSearch) toggleAutoSearch.checked = settings.autoOpenSearchResults;
        if (toggleThinkingSounds) toggleThinkingSounds.checked = settings.thinkingSounds;
    } catch (_) {}
}

function saveSettings() {
    try {
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    } catch (_) {}
}

/**
 * loadServerSettings() — Fetches personalization settings from the server
 * and populates the input fields in the settings panel.
 */
async function loadServerSettings() {
    try {
        const r = await fetch(`${API}/settings`);
        if (!r.ok) return;
        const d = await r.json();
        if (settingUserTitle) settingUserTitle.value = d.user_title || '';
        if (settingTtsVoice) settingTtsVoice.value = d.tts_voice || '';
        if (settingTtsRate) settingTtsRate.value = d.tts_rate || '';
    } catch (_) {}
}

/**
 * saveServerSettings() — Sends the personalization settings to the server.
 */
async function saveServerSettings() {
    if (settingsSaveBtn) settingsSaveBtn.disabled = true;
    try {
        const body = {};
        if (settingUserTitle) body.user_title = settingUserTitle.value;
        if (settingTtsVoice) body.tts_voice = settingTtsVoice.value;
        if (settingTtsRate) body.tts_rate = settingTtsRate.value;
        const r = await fetch(`${API}/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (r.ok) {
            showToast('Settings saved successfully.', 3000);
        } else {
            showToast('Failed to save settings.', 4000);
        }
    } catch (e) {
        showToast('Failed to save settings.', 4000);
    } finally {
        if (settingsSaveBtn) settingsSaveBtn.disabled = false;
    }
}

/* ================================================================
   GREETING
   ================================================================ */

/**
 * setGreeting() — Sets the welcome screen title based on the current
 * time of day.
 *
 * Time ranges:
 *   00:00–11:59 → "Good morning."
 *   12:00–16:59 → "Good afternoon."
 *   17:00–21:59 → "Good evening."
 *   22:00–23:59 → "Burning the midnight oil?" (a fun late-night touch)
 *
 * This is called on page load and when starting a new chat.
 */
function setGreeting() {
    const h = new Date().getHours();
    let g = 'Good evening.';
    if (h < 12) g = 'Good morning.';
    else if (h < 17) g = 'Good afternoon.';
    else if (h >= 22) g = 'Burning the midnight oil?';
    if (welcomeTitle) welcomeTitle.textContent = g;
}

/* ================================================================
   WEBGL ORB INITIALIZATION
   ================================================================ */

/**
 * initOrb() — Creates the animated WebGL orb inside the orbContainer.
 *
 * OrbRenderer is defined in a separate JS file (orb.js). If that file
 * hasn't loaded (e.g., network error), OrbRenderer will be undefined
 * and we skip initialization gracefully.
 *
 * Configuration:
 *   hue: 0                           — The base hue of the orb color
 *   hoverIntensity: 0.3              — How much the orb reacts to mouse hover
 *   backgroundColor: [0.02,0.02,0.06] — Near-black dark blue background (RGB, 0–1 range)
 *
 * The orb's "active" state (pulsing animation) is toggled via
 * orb.setActive(true/false), which we call when TTS starts/stops.
 */
function initOrb() {
    if (typeof OrbRenderer === 'undefined') return;
    try {
        orb = new OrbRenderer(orbContainer, {
            hue: 0,
            hoverIntensity: 0.3,
            backgroundColor: [0.02, 0.02, 0.06]
        });
    } catch (e) { console.warn('Orb init failed:', e); }
}

/* ================================================================
   SPEECH RECOGNITION (Groq Whisper via MediaRecorder)
   ================================================================

   SPEECH-TO-TEXT — SERVER-SIDE WHISPER TRANSCRIPTION
   ----------------------------------------------------------
   Design goals:
   1. Near-perfect transcription accuracy (Groq Whisper large-v3-turbo)
   2. Works on every browser that supports MediaRecorder (Chrome, Edge, Firefox, Safari)
   3. Auto-restart after AI finishes speaking (stream + TTS complete)
   4. Clean, predictable behavior — record → send → transcribe → auto-send

   Flow:
   - User clicks mic → startListening() → MediaRecorder.start()
   - User speaks → speech widget shows "Listening..."
   - User clicks mic again (or silence detected) → stopListening()
   - Audio blob sent to /api/transcribe → Groq Whisper → transcript returned
   - Transcript auto-fills input and sends as message
   - AI responds (stream + TTS) → when TTS queue empty → maybeRestartListening()
   ================================================================ */

/** MediaRecorder instance for voice capture */
let mediaRecorder = null;
/** Collected audio chunks during recording */
let audioChunks = [];
/** Flag to track if MediaRecorder is available */
let mediaRecorderAvailable = false;
/** Silence detection timer — auto-stop after SILENCE_TIMEOUT_MS of no audio */
const SILENCE_TIMEOUT_MS = 2500;
let silenceTimer = null;
/** Audio analyser for silence/volume detection */
let audioAnalyser = null;
let audioContext = null;
let audioStream = null;
let volumeCheckInterval = null;
/** Track consecutive low-volume frames for silence detection */
let lowVolumeFrames = 0;
const LOW_VOLUME_THRESHOLD = 10;     // RMS amplitude below this = "silent"
const SILENCE_FRAME_COUNT = 35;      // ~2.1s of silence at 60fps check interval

/**
 * initSpeech() — Sets up MediaRecorder-based voice capture.
 * Records audio from the mic, sends to backend Groq Whisper for transcription.
 */
function initSpeech() {
    // Check for MediaRecorder support
    if (typeof MediaRecorder === 'undefined' || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        if (micBtn) micBtn.title = 'Voice input not supported in this browser';
        console.warn('[JARVIS] MediaRecorder or getUserMedia not available');
        return;
    }
    mediaRecorderAvailable = true;
    if (micBtn) micBtn.title = 'Click to start voice input (Groq Whisper)';
}

/**
 * startListening() — Activates the microphone and begins recording audio.
 *
 * Guards:
 *   - Does nothing if MediaRecorder isn't available.
 *   - Does nothing if we're currently streaming a response (to avoid
 *     accidentally sending a voice message mid-stream).
 */
async function startListening() {
    if (!mediaRecorderAvailable || isStreaming || isListening) return;

    try {
        // Request microphone access
        audioStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                sampleRate: 16000,
                echoCancellation: true,
                noiseSuppression: true,
            }
        });

        // Determine best supported MIME type
        const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
            .find(t => MediaRecorder.isTypeSupported(t)) || '';

        mediaRecorder = new MediaRecorder(audioStream, mimeType ? { mimeType } : {});
        audioChunks = [];

        mediaRecorder.ondataavailable = (e) => {
            if (e.data && e.data.size > 0) audioChunks.push(e.data);
        };

        mediaRecorder.onstop = async () => {
            // Clean up silence detection
            _stopSilenceDetection();

            if (audioChunks.length === 0) {
                if (speechWidgetText) speechWidgetText.textContent = 'No audio captured';
                setTimeout(() => {
                    if (speechWidget) speechWidget.classList.remove('visible');
                }, 1500);
                return;
            }

            // Show transcribing state
            if (speechWidgetText) speechWidgetText.textContent = 'Transcribing...';

            // Build audio blob
            const ext = mimeType.includes('webm') ? '.webm'
                      : mimeType.includes('ogg') ? '.ogg'
                      : mimeType.includes('mp4') ? '.mp4'
                      : '.webm';
            const blob = new Blob(audioChunks, { type: mimeType || 'audio/webm' });
            audioChunks = [];

            // Don't send very short recordings (likely accidental clicks)
            if (blob.size < 1000) {
                if (speechWidgetText) speechWidgetText.textContent = 'Too short';
                setTimeout(() => {
                    if (speechWidget) speechWidget.classList.remove('visible');
                }, 1500);
                maybeRestartListening();
                return;
            }

            // Send to backend for transcription
            try {
                const form = new FormData();
                form.append('audio', blob, `recording${ext}`);

                const resp = await fetch(`${API}/api/transcribe`, { method: 'POST', body: form });
                const data = await resp.json();

                if (resp.ok && data.text && data.text.trim()) {
                    const transcript = data.text.trim();
                    if (speechWidgetText) speechWidgetText.textContent = transcript;

                    // Auto-send the transcribed message after a brief flash
                    setTimeout(() => {
                        sendMessage(transcript);
                        if (speechWidget) speechWidget.classList.remove('visible');
                    }, 400);
                } else if (resp.ok && (!data.text || !data.text.trim())) {
                    if (speechWidgetText) speechWidgetText.textContent = 'No speech detected';
                    setTimeout(() => {
                        if (speechWidget) speechWidget.classList.remove('visible');
                    }, 2000);
                    maybeRestartListening();
                } else {
                    const errMsg = data.detail || 'Transcription failed';
                    if (speechWidgetText) speechWidgetText.textContent = errMsg;
                    showToast(`Voice: ${errMsg}`, 4000);
                    setTimeout(() => {
                        if (speechWidget) speechWidget.classList.remove('visible');
                    }, 3000);
                    maybeRestartListening();
                }
            } catch (err) {
                console.error('[JARVIS] Transcription error:', err);
                if (speechWidgetText) speechWidgetText.textContent = 'Transcription error';
                showToast('Voice transcription failed. Check server.', 4000);
                setTimeout(() => {
                    if (speechWidget) speechWidget.classList.remove('visible');
                }, 3000);
                maybeRestartListening();
            }
        };

        // Start recording — collect data every 250ms for smoother processing
        mediaRecorder.start(250);
        isListening = true;

        if (micBtn) micBtn.classList.add('listening');
        if (speechWidget) speechWidget.classList.add('visible');
        if (speechWidgetText) speechWidgetText.textContent = 'Listening...';

        /* ── Start live interim transcription (Web Speech API) ──
         * This runs alongside MediaRecorder. It shows words appearing
         * in real-time as the user speaks. Groq Whisper still provides
         * the final accurate transcript for the actual message. */
        _interimFinalText = '';
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (SpeechRecognition) {
            try {
                _interimRecognition = new SpeechRecognition();
                _interimRecognition.continuous = true;
                _interimRecognition.interimResults = true;
                _interimRecognition.lang = 'en-US';
                _interimRecognition.maxAlternatives = 1;

                _interimRecognition.onresult = (event) => {
                    let interim = '';
                    let final = '';
                    for (let i = event.resultIndex; i < event.results.length; i++) {
                        const transcript = event.results[i][0].transcript;
                        if (event.results[i].isFinal) {
                            final += transcript;
                        } else {
                            interim += transcript;
                        }
                    }
                    if (final) _interimFinalText += final;
                    /* Show live words in the speech widget */
                    const displayText = (_interimFinalText + interim).trim();
                    if (speechWidgetText && displayText) {
                        speechWidgetText.textContent = displayText;
                    }
                };

                _interimRecognition.onerror = (event) => {
                    /* Silently ignore — Whisper is the primary transcription engine */
                    console.warn('[JARVIS] Interim speech error (non-fatal):', event.error);
                };

                _interimRecognition.onend = () => {
                    /* Recognition stopped — keep the final interim text visible
                     * until Whisper replaces it with the accurate transcript */
                };

                _interimRecognition.start();
            } catch (e) {
                console.warn('[JARVIS] Web Speech API unavailable:', e);
                _interimRecognition = null;
            }
        }

        // Start silence detection using Web Audio API
        _startSilenceDetection(audioStream);

    } catch (err) {
        console.error('[JARVIS] Microphone access error:', err);
        isListening = false;
        if (micBtn) micBtn.classList.remove('listening');
        if (speechWidget) speechWidget.classList.remove('visible');

        if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
            if (micBtn) micBtn.title = 'Microphone access denied. Allow in browser settings.';
            showToast('Microphone access denied. Please allow it in browser settings.', 5000);
        } else {
            showToast('Could not access microphone: ' + err.message, 4000);
        }
    }
}

/**
 * _startSilenceDetection(stream) — Uses Web Audio API to monitor mic volume.
 * When the user stops speaking (sustained low volume), auto-stops recording.
 */
function _startSilenceDetection(stream) {
    try {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const source = audioContext.createMediaStreamSource(stream);
        audioAnalyser = audioContext.createAnalyser();
        audioAnalyser.fftSize = 512;
        source.connect(audioAnalyser);

        const dataArray = new Uint8Array(audioAnalyser.frequencyBinCount);
        lowVolumeFrames = 0;

        // Check volume every 60ms
        volumeCheckInterval = setInterval(() => {
            if (!audioAnalyser || !isListening) return;
            audioAnalyser.getByteTimeDomainData(dataArray);

            // Calculate RMS (root mean square) volume
            let sum = 0;
            for (let i = 0; i < dataArray.length; i++) {
                const val = (dataArray[i] - 128) / 128;
                sum += val * val;
            }
            const rms = Math.sqrt(sum / dataArray.length) * 100;

            if (rms < LOW_VOLUME_THRESHOLD) {
                lowVolumeFrames++;
            } else {
                lowVolumeFrames = 0;
                // User is speaking — update widget
                if (speechWidgetText && speechWidgetText.textContent === 'Listening...') {
                    speechWidgetText.textContent = 'Listening... 🎤';
                }
            }

            // Auto-stop after sustained silence (only if we've recorded some audio)
            if (lowVolumeFrames >= SILENCE_FRAME_COUNT && audioChunks.length > 2) {
                stopListening();
            }
        }, 60);
    } catch (e) {
        console.warn('[JARVIS] Silence detection unavailable:', e);
        // Fallback: simple timeout-based stop
        silenceTimer = setTimeout(() => {
            if (isListening) stopListening();
        }, 15000);  // Max 15s recording without silence detection
    }
}

/**
 * _stopSilenceDetection() — Cleans up silence detection resources.
 */
function _stopSilenceDetection() {
    if (volumeCheckInterval) { clearInterval(volumeCheckInterval); volumeCheckInterval = null; }
    if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
    if (audioContext) {
        try { audioContext.close(); } catch (_) {}
        audioContext = null;
    }
    audioAnalyser = null;
    lowVolumeFrames = 0;
}

/**
 * stopListening() — Stops recording and triggers transcription.
 *
 * Called when:
 *   - The user clicks the mic button again (manual toggle off).
 *   - Silence is detected (auto-stop).
 *   - A new message is being sent.
 */
function stopListening() {
    if (!isListening) return;
    isListening = false;

    if (micBtn) micBtn.classList.remove('listening');

    /* Stop live interim recognition (Web Speech API) */
    if (_interimRecognition) {
        try { _interimRecognition.stop(); } catch (_) {}
        _interimRecognition = null;
    }

    // Stop the MediaRecorder — this triggers .onstop which sends audio for transcription
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        try { mediaRecorder.stop(); } catch (_) {}
    }

    // Release the microphone stream
    if (audioStream) {
        audioStream.getTracks().forEach(t => t.stop());
        audioStream = null;
    }

    _stopSilenceDetection();
}

/**
 * maybeRestartListening() — If autoListenMode is on and the AI response
 * (stream + TTS) is fully complete, restart listening after a short delay.
 * Called from: sendMessage finally block, TTSPlayer.onPlaybackComplete.
 */
function maybeRestartListening() {
    if (!autoListenMode || !mediaRecorderAvailable) return;
    if (isStreaming) return;
    if (ttsPlayer && (ttsPlayer.playing || ttsPlayer.queue.length > 0)) return;
    setTimeout(() => {
        if (autoListenMode && !isStreaming && !isListening && mediaRecorderAvailable) {
            startListening();
        }
    }, SPEECH_RESTART_DELAY_MS);
}

/* ================================================================
   BACKEND HEALTH CHECK
   ================================================================ */

/**
 * checkHealth() — Pings the backend's /health endpoint to determine
 * if the server is running and healthy.
 *
 * Updates the status indicator in the UI:
 *   - Green dot + "Online"  if the server responds with { status: "healthy" }
 *   - Red dot   + "Offline" if the request fails or returns unhealthy
 *
 * Uses AbortSignal.timeout(5000) to avoid waiting forever if the
 * server is down — the request will automatically abort after 5 seconds.
 */
async function checkHealth() {
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000);
        const r = await fetch(`${API}/health`, { signal: controller.signal });
        clearTimeout(timeoutId);
        const d = await r.json().catch(() => null);
        const ok = d && (d.status === 'healthy' || d.status === 'degraded');
        if (statusDot) statusDot.classList.toggle('offline', !ok);
        if (statusText) statusText.textContent = ok ? 'Online' : 'Offline';
    } catch (e) {
        if (statusDot) statusDot.classList.add('offline');
        if (statusText) statusText.textContent = 'Offline';
        if (typeof console !== 'undefined' && console.warn) console.warn('[Health] Check failed:', e);
    }
}

/**
 * showToast(msg, durationMs) — Brief feedback for errors/status.
 * Auto-dismisses after durationMs (default 5000).
 */
function showToast(msg, durationMs = 5000) {
    if (!toastContainer || !msg) return;
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    toastContainer.appendChild(el);
    el.offsetHeight;   // Force reflow for animation
    el.classList.add('toast-visible');
    const t = setTimeout(() => {
        el.classList.remove('toast-visible');
        setTimeout(() => el.remove(), 300);
    }, durationMs);
    el.addEventListener('click', () => { clearTimeout(t); el.classList.remove('toast-visible'); setTimeout(() => el.remove(), 300); });
}

/* ================================================================
   EVENT BINDING
   ================================================================
   All user-interaction event listeners are centralized here for
   clarity. This function is called once during init().
   ================================================================ */

/**
 * bindEvents() — Wires up all click, keydown, and input event
 * listeners for the UI.
 */
function bindEvents() {
    // SEND BUTTON — Send the message when clicked (if not already streaming)
    if (sendBtn) sendBtn.addEventListener('click', () => { if (!isStreaming) sendMessage(); });

    // ENTER KEY — Send on Enter (but allow Shift+Enter for new lines)
    if (messageInput) messageInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!isStreaming) sendMessage(); }
    });

    // INPUT CHANGE — Auto-resize the textarea and show character count for long messages
    if (messageInput) messageInput.addEventListener('input', () => {
        autoResizeInput();
        const len = messageInput.value.length;
        if (charCount) charCount.textContent = len > 100 ? `${len.toLocaleString()} / 32,000` : '';
    });

    // MIC BUTTON — Toggle speech recognition. When ON: auto mode — listen, stop on send, restart after AI+TTS done.
    if (micBtn) micBtn.addEventListener('click', () => {
        if (isListening) {
            autoListenMode = false;
            stopListening();
            if (micBtn) micBtn.classList.remove('auto-listen');
        } else {
            autoListenMode = true;
            speechErrorRetryCount = 0;   // Reset retry count on fresh start
            if (micBtn) {
                micBtn.classList.add('auto-listen');
                micBtn.title = 'Voice input — click to stop auto-listen';
            }
            startListening();
        }
    });

    // TTS BUTTON — Toggle text-to-speech on/off
    if (ttsBtn) ttsBtn.addEventListener('click', () => {
        if (ttsPlayer) ttsPlayer.enabled = !ttsPlayer.enabled;
        ttsBtn.classList.toggle('tts-active', ttsPlayer && ttsPlayer.enabled);
        if (ttsPlayer && !ttsPlayer.enabled) ttsPlayer.stop();
    });

    // NEW CHAT BUTTON — Reset the conversation
    if (newChatBtn) newChatBtn.addEventListener('click', newChat);

    // MODE TOGGLE BUTTONS — Switch between Jarvis, General, and Realtime modes
    if (btnJarvis)   btnJarvis.addEventListener('click',   () => setMode('jarvis'));
    if (btnGeneral)  btnGeneral.addEventListener('click',   () => setMode('general'));
    if (btnRealtime) btnRealtime.addEventListener('click',  () => setMode('realtime'));

    // QUICK-ACTION CHIPS — Predefined messages on the welcome screen
    // Each chip has a data-msg attribute containing the message to send
    document.querySelectorAll('.chip').forEach(c => {
        c.addEventListener('click', () => { if (!isStreaming) sendMessage(c.dataset.msg); });
    });

    // SEARCH RESULTS WIDGET — Toggle panel open/close from header button; close from panel button
    if (searchResultsToggle) {
        searchResultsToggle.addEventListener('click', () => {
            if (searchResultsWidget) { searchResultsWidget.classList.toggle('open'); updatePanelOverlay(); }
        });
    }
    if (searchResultsClose && searchResultsWidget) {
        searchResultsClose.addEventListener('click', () => { searchResultsWidget.classList.remove('open'); updatePanelOverlay(); });
    }
    // ACTIVITY PANEL — Toggle open/close from header button; close from panel button
    if (activityToggle) {
        activityToggle.addEventListener('click', () => {
            if (activityPanel) { activityPanel.classList.toggle('open'); updatePanelOverlay(); }
        });
    }
    if (activityClose && activityPanel) {
        activityClose.addEventListener('click', () => { activityPanel.classList.remove('open'); updatePanelOverlay(); });
    }
    // Panels close ONLY via their X button — overlay does not close on click
    // SETTINGS
    if (settingsBtn && settingsPanel) {
        settingsBtn.addEventListener('click', () => {
            settingsPanel.classList.toggle('open');
            updatePanelOverlay();
            if (settingsPanel.classList.contains('open')) loadServerSettings();
        });
    }
    if (settingsClose && settingsPanel) {
        settingsClose.addEventListener('click', () => {
            settingsPanel.classList.remove('open');
            updatePanelOverlay();
        });
    }
    if (toggleAutoActivity) {
        toggleAutoActivity.addEventListener('change', () => {
            settings.autoOpenActivity = toggleAutoActivity.checked;
            saveSettings();
        });
    }
    if (toggleAutoSearch) {
        toggleAutoSearch.addEventListener('change', () => {
            settings.autoOpenSearchResults = toggleAutoSearch.checked;
            saveSettings();
        });
    }
    if (toggleThinkingSounds) {
        toggleThinkingSounds.addEventListener('change', () => {
            settings.thinkingSounds = toggleThinkingSounds.checked;
            saveSettings();
        });
    }
    if (settingsSaveBtn) {
        settingsSaveBtn.addEventListener('click', () => saveServerSettings());
    }

    // KEYBOARD SHORTCUTS
    document.addEventListener('keydown', e => {
        // Ctrl+N or Cmd+N — New chat
        if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
            e.preventDefault();
            newChat();
        }
        // Ctrl+Shift+M — Toggle mic
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'M') {
            e.preventDefault();
            if (micBtn) micBtn.click();
        }
        // Ctrl+Shift+S — Toggle TTS
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'S') {
            e.preventDefault();
            if (ttsBtn) ttsBtn.click();
        }
        // Ctrl+Shift+H — Toggle chat history   
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'H') {
            e.preventDefault();
            toggleHistoryPanel();
        }
        // Escape — Close any open panel
        if (e.key === 'Escape') {
            if (activityPanel && activityPanel.classList.contains('open')) activityPanel.classList.remove('open');
            if (searchResultsWidget && searchResultsWidget.classList.contains('open')) searchResultsWidget.classList.remove('open');
            if (settingsPanel && settingsPanel.classList.contains('open')) settingsPanel.classList.remove('open');
            const histPanel = document.getElementById('history-panel');
            if (histPanel && histPanel.classList.contains('open')) histPanel.classList.remove('open');
            updatePanelOverlay();
        }
    });

    // HISTORY BUTTON
    const histBtn = document.getElementById('history-btn');
    if (histBtn) histBtn.addEventListener('click', toggleHistoryPanel);
    const histClose = document.getElementById('history-close');
    if (histClose) histClose.addEventListener('click', () => {
        const p = document.getElementById('history-panel');
        if (p) p.classList.remove('open');
        updatePanelOverlay();
    });

    // FILE UPLOAD — supports 30+ file types
    const ALLOWED_EXTENSIONS = '.txt,.md,.csv,.log,.json,.xml,.yaml,.yml,.ini,.cfg,.env,.toml,.py,.js,.jsx,.ts,.tsx,.html,.htm,.css,.scss,.sass,.less,.php,.java,.c,.cpp,.h,.hpp,.cs,.go,.rs,.rb,.swift,.kt,.sql,.sh,.bash,.bat,.ps1,.r,.dart,.lua,.vue,.svelte,.pdf,.pptx,.docx,.rtf,.tex';

    async function handleFileUpload(file) {
        if (!file) return;
        const ext = '.' + file.name.split('.').pop().toLowerCase();
        if (!ALLOWED_EXTENSIONS.includes(ext)) {
            showToast(`Unsupported file type: ${ext}`, 5000);
            return;
        }
        if (file.size > 5 * 1024 * 1024) {
            showToast('File too large (max 5MB)', 5000);
            return;
        }

        // Show uploading toast
        showToast(`Uploading ${file.name}...`, 10000);

        const form = new FormData();
        form.append('file', file);
        // Send session_id so backend can link file context to this session
        if (typeof sessionId !== 'undefined' && sessionId) {
            form.append('session_id', sessionId);
        }
        try {
            const r = await fetch(`${API}/chat/upload`, { method: 'POST', body: form });
            const d = await r.json();
            if (r.ok) {
                // Build rich summary
                const icon = d.file_type === 'pdf' ? '📄' : d.file_type === 'pptx' ? '📊' : d.file_type === 'docx' ? '📝' : d.file_type === 'code' ? '💻' : '📁';
                let meta = `${d.words.toLocaleString()} words`;
                if (d.pages) meta += ` · ${d.pages} pages`;
                if (d.slides) meta += ` · ${d.slides} slides`;
                if (d.indexed) meta += ' · Indexed';

                // Show upload card in chat
                hideWelcome();
                const msg = document.createElement('div');
                msg.className = 'message assistant';
                msg.innerHTML = `
                    <div class="msg-avatar">${AVATAR_ICON_ASSISTANT}</div>
                    <div class="msg-body">
                        <div class="msg-label">Jarvis</div>
                        <div class="msg-content upload-card">
                            <div class="upload-card-icon">${icon}</div>
                            <div class="upload-card-info">
                                <div class="upload-card-name">${escapeHtml(d.filename)}</div>
                                <div class="upload-card-meta">${meta}</div>
                            </div>
                        </div>
                        <div class="msg-content" style="margin-top:6px;font-size:0.82rem;color:var(--text-dim);">
                            File uploaded and indexed. Ask me anything about it!
                        </div>
                    </div>`;
                if (chatMessages) chatMessages.appendChild(msg);
                scrollToBottom();
                showToast(`${icon} ${d.filename} uploaded successfully`, 3000);
            } else {
                showToast(d.detail || 'Upload failed', 5000);
            }
        } catch (e) {
            showToast('Upload failed: ' + e.message, 5000);
        }
    }

    const uploadBtn = document.getElementById('upload-btn');
    if (uploadBtn) uploadBtn.addEventListener('click', () => {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = ALLOWED_EXTENSIONS;
        input.addEventListener('change', () => handleFileUpload(input.files[0]));
        input.click();
    });

    // FILE MANAGEMENT MODAL
    const filesBtn = document.getElementById('files-btn');
    const filesModal = document.getElementById('files-modal');
    const filesModalBackdrop = document.getElementById('files-modal-backdrop');
    const filesModalClose = document.getElementById('files-modal-close');
    const filesList = document.getElementById('files-list');

    async function loadUploadedFiles() {
        try {
            const r = await fetch(`${API}/chat/files`);
            const d = await r.json();
            if (r.ok && d.files && d.files.length > 0) {
                filesList.innerHTML = d.files.map(f => `
                    <div class="file-item">
                        <div class="file-icon">${f.file_type === 'pdf' ? '📄' : f.file_type === 'pptx' ? '📊' : f.file_type === 'docx' ? '📝' : f.file_type === 'code' ? '💻' : '📁'}</div>
                        <div class="file-info">
                            <div class="file-name">${escapeHtml(f.original_name || f.filename)}</div>
                            <div class="file-meta">${f.words.toLocaleString()} words · ${f.file_type}</div>
                        </div>
                        <div class="file-actions">
                            <button class="file-use-btn" data-filename="${escapeHtml(f.filename)}" title="Use in next message">Use</button>
                            <button class="file-delete-btn" data-filename="${escapeHtml(f.filename)}" title="Delete file">🗑️</button>
                        </div>
                    </div>
                `).join('');
                
                // Add event listeners for use and delete buttons
                filesList.querySelectorAll('.file-use-btn').forEach(btn => {
                    btn.addEventListener('click', async (e) => {
                        const filename = e.target.dataset.filename;
                        try {
                            const res = await fetch(`${API}/chat/files/${filename}/use`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ session_id: sessionId || 'global' })
                            });
                            const result = await res.json();
                            if (res.ok) {
                                showToast(`File '${filename}' will be used in next message`, 3000);
                            } else {
                                showToast(result.detail || 'Failed to use file', 3000);
                            }
                        } catch (err) {
                            showToast('Failed to use file', 3000);
                        }
                    });
                });
                
                filesList.querySelectorAll('.file-delete-btn').forEach(btn => {
                    btn.addEventListener('click', async (e) => {
                        const filename = e.target.dataset.filename;
                        if (!confirm(`Delete file '${filename}'?`)) return;
                        try {
                            const res = await fetch(`${API}/chat/files/${filename}`, { method: 'DELETE' });
                            if (res.ok) {
                                showToast(`File '${filename}' deleted`, 3000);
                                loadUploadedFiles(); // Refresh list
                            } else {
                                const result = await res.json();
                                showToast(result.detail || 'Failed to delete file', 3000);
                            }
                        } catch (err) {
                            showToast('Failed to delete file', 3000);
                        }
                    });
                });
            } else {
                filesList.innerHTML = '<div class="files-empty">No files uploaded yet.</div>';
            }
        } catch (e) {
            filesList.innerHTML = '<div class="files-empty">Failed to load files.</div>';
        }
    }

    if (filesBtn) {
        filesBtn.addEventListener('click', () => {
            loadUploadedFiles();
            filesModal.setAttribute('aria-hidden', 'false');
            document.body.style.overflow = 'hidden';
        });
    }

    if (filesModalClose) {
        filesModalClose.addEventListener('click', () => {
            filesModal.setAttribute('aria-hidden', 'true');
            document.body.style.overflow = '';
        });
    }

    if (filesModalBackdrop) {
        filesModalBackdrop.addEventListener('click', () => {
            filesModal.setAttribute('aria-hidden', 'true');
            document.body.style.overflow = '';
        });
    }

    // REMINDER MODAL
    const reminderBtn = document.getElementById('reminder-btn');
    const reminderModal = document.getElementById('reminder-modal');
    const reminderModalBackdrop = document.getElementById('reminder-modal-backdrop');
    const reminderModalClose = document.getElementById('reminder-modal-close');
    const remindersList = document.getElementById('reminders-list');
    const reminderCreateBtn = document.getElementById('reminder-create-btn');

    async function loadReminders() {
        try {
            const r = await fetch(`${API}/chat/reminders`);
            const d = await r.json();
            if (r.ok && d.reminders && d.reminders.length > 0) {
                remindersList.innerHTML = d.reminders.map(rem => `
                    <div class="reminder-item">
                        <div class="reminder-info">
                            <div class="reminder-message">${escapeHtml(rem.message)}</div>
                            <div class="reminder-meta">${new Date(rem.remind_at).toLocaleString()} · ${rem.repeat}</div>
                        </div>
                        <div class="reminder-actions">
                            <button class="reminder-delete-btn" data-id="${rem.id}" title="Delete">🗑️</button>
                        </div>
                    </div>
                `).join('');
                
                remindersList.querySelectorAll('.reminder-delete-btn').forEach(btn => {
                    btn.addEventListener('click', async (e) => {
                        const id = e.target.dataset.id;
                        try {
                            const res = await fetch(`${API}/chat/reminders/${id}`, { method: 'DELETE' });
                            if (res.ok) {
                                showToast('Reminder deleted', 3000);
                                loadReminders();
                            }
                        } catch (err) {
                            showToast('Failed to delete reminder', 3000);
                        }
                    });
                });
            } else {
                remindersList.innerHTML = '<div class="reminders-empty">No reminders set.</div>';
            }
        } catch (e) {
            remindersList.innerHTML = '<div class="reminders-empty">Failed to load reminders.</div>';
        }
    }

    if (reminderCreateBtn) {
        reminderCreateBtn.addEventListener('click', async () => {
            const message = document.getElementById('reminder-message').value;
            const datetime = document.getElementById('reminder-datetime').value;
            const repeat = document.getElementById('reminder-repeat').value;
            const sound = document.getElementById('reminder-sound').value;
            const speak = document.getElementById('reminder-speak').checked;
            
            if (!message) {
                showToast('Please enter a reminder message', 3000);
                return;
            }
            
            try {
                let res, d;
                
                // If datetime is provided, use manual creation; otherwise use AI
                if (datetime) {
                    res = await fetch(`${API}/chat/reminders`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            message,
                            remind_at: new Date(datetime).toISOString(),
                            repeat,
                            sound,
                            speak,
                            active: true
                        })
                    });
                } else {
                    // Use AI to parse natural language reminder
                    res = await fetch(`${API}/chat/reminders/ai`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ message })
                    });
                }
                
                d = await res.json();
                if (res.ok) {
                    const timeStr = d.reminder ? new Date(d.reminder.remind_at).toLocaleString() : '';
                    showToast(`Reminder created for ${timeStr}`, 3000);
                    loadReminders();
                    document.getElementById('reminder-message').value = '';
                    document.getElementById('reminder-datetime').value = '';
                } else {
                    showToast(d.detail || 'Failed to create reminder', 3000);
                }
            } catch (err) {
                showToast('Failed to create reminder', 3000);
            }
        });
    }

    if (reminderBtn) {
        reminderBtn.addEventListener('click', () => {
            loadReminders();
            reminderModal.setAttribute('aria-hidden', 'false');
            document.body.style.overflow = 'hidden';
        });
    }

    if (reminderModalClose) {
        reminderModalClose.addEventListener('click', () => {
            reminderModal.setAttribute('aria-hidden', 'true');
            document.body.style.overflow = '';
        });
    }

    if (reminderModalBackdrop) {
        reminderModalBackdrop.addEventListener('click', () => {
            reminderModal.setAttribute('aria-hidden', 'true');
            document.body.style.overflow = '';
        });
    }

    // INTEGRATIONS MODAL
    const integrationsBtn = document.getElementById('integrations-btn');
    const integrationsModal = document.getElementById('integrations-modal');
    const integrationsModalBackdrop = document.getElementById('integrations-modal-backdrop');
    const integrationsModalClose = document.getElementById('integrations-modal-close');

    // Email integration
    const emailConfigBtn = document.getElementById('email-config-btn');
    const emailForm = document.getElementById('email-form');
    const emailSendForm = document.getElementById('email-send-form');
    const emailSaveBtn = document.getElementById('email-save-btn');
    const emailSendBtn = document.getElementById('email-send-btn');

    async function loadEmailConfig() {
        try {
            const r = await fetch(`${API}/integrations/email/config`);
            const d = await r.json();
            if (d.configured) {
                document.getElementById('email-status').textContent = 'Configured';
                emailConfigBtn.textContent = 'Send Email';
            } else {
                document.getElementById('email-status').textContent = 'Not configured';
                emailConfigBtn.textContent = 'Configure';
            }
        } catch (e) {
            console.error('Failed to load email config:', e);
        }
    }

    if (emailConfigBtn) {
        emailConfigBtn.addEventListener('click', () => {
            if (emailForm.style.display === 'none') {
                emailForm.style.display = 'flex';
                emailSendForm.style.display = 'none';
            } else {
                emailForm.style.display = 'none';
            }
        });
    }

    if (emailSaveBtn) {
        emailSaveBtn.addEventListener('click', async () => {
            const smtp_host = document.getElementById('email-smtp-host').value;
            const smtp_port = parseInt(document.getElementById('email-smtp-port').value) || 587;
            const username = document.getElementById('email-username').value;
            const password = document.getElementById('email-password').value;
            const from_email = document.getElementById('email-from').value;

            if (!smtp_host || !username || !password) {
                showToast('Please fill in required fields', 3000);
                return;
            }

            try {
                const res = await fetch(`${API}/integrations/email/config`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ smtp_host, smtp_port, username, password, from_email })
                });
                const d = await res.json();
                if (res.ok) {
                    showToast('Email configured successfully', 3000);
                    loadEmailConfig();
                    emailForm.style.display = 'none';
                    emailSendForm.style.display = 'flex';
                } else {
                    showToast(d.detail || 'Failed to configure email', 3000);
                }
            } catch (e) {
                showToast('Failed to configure email', 3000);
            }
        });
    }

    if (emailSendBtn) {
        emailSendBtn.addEventListener('click', async () => {
            const to = document.getElementById('email-to').value;
            const subject = document.getElementById('email-subject').value;
            const body = document.getElementById('email-body').value;

            if (!to || !subject || !body) {
                showToast('Please fill in all fields', 3000);
                return;
            }

            try {
                const res = await fetch(`${API}/integrations/email/send`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ to, subject, body })
                });
                const d = await res.json();
                if (res.ok) {
                    showToast('Email sent successfully', 3000);
                    document.getElementById('email-to').value = '';
                    document.getElementById('email-subject').value = '';
                    document.getElementById('email-body').value = '';
                } else {
                    showToast(d.detail || 'Failed to send email', 3000);
                }
            } catch (e) {
                showToast('Failed to send email', 3000);
            }
        });
    }

    // Calendar integration
    const calendarAuthBtn = document.getElementById('calendar-auth-btn');
    const calendarForm = document.getElementById('calendar-form');
    const calendarEventsForm = document.getElementById('calendar-events-form');
    const calendarConnectBtn = document.getElementById('calendar-connect-btn');
    const calendarRefreshBtn = document.getElementById('calendar-refresh-btn');

    async function loadCalendarConfig() {
        try {
            const r = await fetch(`${API}/integrations/calendar/config`);
            const d = await r.json();
            if (d.configured) {
                document.getElementById('calendar-status').textContent = 'Connected';
                calendarAuthBtn.textContent = 'View Events';
                loadCalendarEvents();
            } else {
                document.getElementById('calendar-status').textContent = 'Not configured';
                calendarAuthBtn.textContent = 'Connect';
            }
        } catch (e) {
            console.error('Failed to load calendar config:', e);
        }
    }

    async function loadCalendarEvents() {
        const eventsList = document.getElementById('calendar-events-list');
        eventsList.innerHTML = '<div class="calendar-empty">Loading events...</div>';
        
        try {
            const res = await fetch(`${API}/integrations/calendar/events`);
            const d = await res.json();
            
            if (res.ok && d.events && d.events.length > 0) {
                eventsList.innerHTML = d.events.slice(0, 10).map(ev => `
                    <div class="calendar-event-item">
                        <div class="calendar-event-title">${escapeHtml(ev.summary)}</div>
                        <div class="calendar-event-time">${new Date(ev.start).toLocaleString()} - ${new Date(ev.end).toLocaleString()}</div>
                    </div>
                `).join('');
            } else {
                eventsList.innerHTML = '<div class="calendar-empty">No upcoming events</div>';
            }
        } catch (e) {
            eventsList.innerHTML = '<div class="calendar-empty">Failed to load events</div>';
        }
    }

    if (calendarAuthBtn) {
        calendarAuthBtn.addEventListener('click', () => {
            if (calendarForm.style.display === 'none') {
                calendarForm.style.display = 'flex';
                calendarEventsForm.style.display = 'none';
            } else {
                calendarForm.style.display = 'none';
                calendarEventsForm.style.display = 'flex';
                loadCalendarEvents();
            }
        });
    }

    if (calendarConnectBtn) {
        calendarConnectBtn.addEventListener('click', async () => {
            // Open Google OAuth flow
            window.open(`${API}/integrations/calendar/auth`, '_blank');
        });
    }

    if (calendarRefreshBtn) {
        calendarRefreshBtn.addEventListener('click', loadCalendarEvents);
    }

    // Notion integration
    const notionConfigBtn = document.getElementById('notion-config-btn');
    const notionForm = document.getElementById('notion-form');
    const notionSaveBtn = document.getElementById('notion-save-btn');

    async function loadNotionConfig() {
        try {
            const r = await fetch(`${API}/integrations/notion/config`);
            const d = await r.json();
            if (d.configured) {
                document.getElementById('notion-status').textContent = 'Configured';
                notionConfigBtn.textContent = 'Configured';
            } else {
                document.getElementById('notion-status').textContent = 'Not configured';
                notionConfigBtn.textContent = 'Configure';
            }
        } catch (e) {
            console.error('Failed to load notion config:', e);
        }
    }

    if (notionConfigBtn) {
        notionConfigBtn.addEventListener('click', () => {
            if (notionForm.style.display === 'none') {
                notionForm.style.display = 'flex';
            } else {
                notionForm.style.display = 'none';
            }
        });
    }

    if (notionSaveBtn) {
        notionSaveBtn.addEventListener('click', async () => {
            const api_key = document.getElementById('notion-api-key').value;
            const database_id = document.getElementById('notion-database-id').value;

            if (!api_key || !database_id) {
                showToast('Please fill in all fields', 3000);
                return;
            }

            try {
                const res = await fetch(`${API}/integrations/notion/config`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ api_key, database_id })
                });
                const d = await res.json();
                if (res.ok) {
                    showToast('Notion configured successfully', 3000);
                    loadNotionConfig();
                    notionForm.style.display = 'none';
                } else {
                    showToast(d.detail || 'Failed to configure Notion', 3000);
                }
            } catch (e) {
                showToast('Failed to configure Notion', 3000);
            }
        });
    }

    if (integrationsBtn) {
        integrationsBtn.addEventListener('click', () => {
            loadEmailConfig();
            loadCalendarConfig();
            loadNotionConfig();
            integrationsModal.setAttribute('aria-hidden', 'false');
            document.body.style.overflow = 'hidden';
        });
    }

    if (integrationsModalClose) {
        integrationsModalClose.addEventListener('click', () => {
            integrationsModal.setAttribute('aria-hidden', 'true');
            document.body.style.overflow = '';
        });
    }

    if (integrationsModalBackdrop) {
        integrationsModalBackdrop.addEventListener('click', () => {
            integrationsModal.setAttribute('aria-hidden', 'true');
            document.body.style.overflow = '';
        });
    }

    // DRAG AND DROP — on the entire page for full coverage
    const dropTarget = document.getElementById('chat-area') || document.querySelector('main') || document.body;
    let dragCounter = 0;

    document.addEventListener('dragenter', (e) => {
        e.preventDefault();
        dragCounter++;
        dropTarget.classList.add('drag-over');
    });
    document.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dragCounter--;
        if (dragCounter <= 0) {
            dragCounter = 0;
            dropTarget.classList.remove('drag-over');
        }
    });
    document.addEventListener('dragover', (e) => {
        e.preventDefault();
    });
    document.addEventListener('drop', (e) => {
        e.preventDefault();
        dragCounter = 0;
        dropTarget.classList.remove('drag-over');
        const file = e.dataTransfer.files && e.dataTransfer.files[0];
        if (file) handleFileUpload(file);
    });
}

/**
 * autoResizeInput() — Dynamically adjusts the textarea height to fit
 * its content, up to a maximum of 120px.
 *
 * How it works:
 *   1. Reset height to 'auto' so scrollHeight reflects actual content height.
 *   2. Set height to the smaller of scrollHeight or 120px.
 *   This creates a textarea that grows as the user types but doesn't
 *   take over the whole screen for very long messages.
 */
function autoResizeInput() {
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
}

/* ================================================================
   MODE SWITCH (General ↔ Realtime)
   ================================================================
   The app supports two AI modes, each hitting a different backend
   endpoint:
     - "General"  → /chat/stream         (standard LLM pipeline)
     - "Realtime" → /chat/realtime/stream (realtime/low-latency pipeline)

   The mode is purely a UI + routing concern — the frontend logic for
   streaming and rendering is identical for both modes.
   ================================================================ */

/**
 * updatePanelOverlay() — Shows/hides the backdrop overlay when any side panel is open.
 */
function updatePanelOverlay() {
    if (!panelOverlay) return;
    const anyOpen = (activityPanel && activityPanel.classList.contains('open')) ||
        (searchResultsWidget && searchResultsWidget.classList.contains('open')) ||
        (settingsPanel && settingsPanel.classList.contains('open'));
    panelOverlay.classList.toggle('visible', !!anyOpen);
}

/**
 * setMode(mode) — Switches the active mode and updates the UI.
 *
 * @param {string} mode - Either 'general' or 'realtime'.
 *
 * Updates:
 *   - currentMode variable (used when sending messages)
 *   - Button active states (highlights the selected button)
 *   - Slider position (slides the pill indicator left or right)
 */
function setMode(mode) {
    currentMode = mode;
    if (btnJarvis)   btnJarvis.classList.toggle('active', mode === 'jarvis');
    if (btnGeneral)  btnGeneral.classList.toggle('active', mode === 'general');
    if (btnRealtime) btnRealtime.classList.toggle('active', mode === 'realtime');
    if (modeSlider) {
        modeSlider.classList.remove('center', 'right');
        if (mode === 'general') modeSlider.classList.add('center');
        else if (mode === 'realtime') modeSlider.classList.add('right');
        /* jarvis: no class = left position */
    }
    // Activity toggle always visible — panel shows flow in all modes
    if (activityToggle) activityToggle.style.display = '';
}

/* ================================================================
   NEW CHAT
   ================================================================ */

/**
 * newChat() — Resets the entire conversation to a fresh state.
 *
 * Steps:
 *   1. Stop any playing TTS audio.
 *   2. Clear the session ID (server will create a new one on next message).
 *   3. Clear all messages from the chat container.
 *   4. Re-create and display the welcome screen.
 *   5. Clear the input field and reset its size.
 *   6. Update the greeting text (in case time-of-day changed).
 */
function newChat() {
    if (ttsPlayer) ttsPlayer.stop();
    sessionId = null;
    if (chatMessages) chatMessages.innerHTML = '';
    chatMessages.appendChild(createWelcome());
    messageInput.value = '';
    autoResizeInput();
    setGreeting();
    if (searchResultsWidget) searchResultsWidget.classList.remove('open');
    if (searchResultsToggle) searchResultsToggle.style.display = 'none';
    if (activityPanel) activityPanel.classList.remove('open');
    if (settingsPanel) settingsPanel.classList.remove('open');
    if (activityToggle) activityToggle.style.display = 'none';
    if (activityList) {
        activityList.innerHTML = '<div class="activity-empty" id="activity-empty">Send a message to see the flow here.</div>';
    }
    updatePanelOverlay();
}

/**
 * createWelcome() — Builds and returns the welcome screen DOM element.
 *
 * @returns {HTMLDivElement} The welcome screen element, ready to be
 *                           appended to the chat container.
 *
 * The welcome screen includes:
 *   - A decorative SVG icon
 *   - A time-based greeting (same logic as setGreeting)
 *   - A subtitle prompt ("How may I assist you today?")
 *   - Quick-action chip buttons with predefined messages
 *
 * The chip buttons get their own click listeners here because they
 * are dynamically created (not present in the original HTML).
 */
function createWelcome() {
    const h = new Date().getHours();
    let g = 'Good evening.';
    if (h < 12) g = 'Good morning.';
    else if (h < 17) g = 'Good afternoon.';
    else if (h >= 22) g = 'Burning the midnight oil?';

    const div = document.createElement('div');
    div.className = 'welcome-screen';
    div.id = 'welcome-screen';
    div.innerHTML = `
        <div class="welcome-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
        </div>
        <h2 class="welcome-title">${g}</h2>
        <p class="welcome-sub">How may I assist you today?</p>
        <div class="welcome-chips">
            <button class="chip" data-msg="What can you do?">What can you do?</button>
            <button class="chip" data-msg="Open YouTube for me">Open YouTube</button>
            <button class="chip" data-msg="Tell me a fun fact">Fun fact</button>
            <button class="chip" data-msg="Play some music">Play music</button>
        </div>`;

    // Attach click handlers to the dynamically created chip buttons
    div.querySelectorAll('.chip').forEach(c => {
        c.addEventListener('click', () => { if (!isStreaming) sendMessage(c.dataset.msg); });
    });
    return div;
}

/* ================================================================
   MESSAGE RENDERING
   ================================================================
   These functions build the chat message DOM elements. Each message
   consists of:
     - An avatar circle ("J" for Jarvis, "U" for user)
     - A body containing a label (name + mode) and the content text

   The structure mirrors common chat UIs (Slack, Discord, ChatGPT).
   ================================================================ */

/**
 * isUrlLike(str) — True if the string looks like a URL or encoded path (not a readable title/snippet).
 */
function isUrlLike(str) {
    if (!str || typeof str !== 'string') return false;
    const s = str.trim();
    return s.length > 40 && (/^https?:\/\//i.test(s) || /\%2f|\%3a|\.com\/|\.org\//i.test(s));
}

/**
 * friendlyUrlLabel(url) — Short, readable label for a URL (domain + path hint) for display.
 */
function friendlyUrlLabel(url) {
    if (!url || typeof url !== 'string') return 'View source';
    try {
        const u = new URL(url.startsWith('http') ? url : 'https://' + url);
        const host = u.hostname.replace(/^www\./, '');
        const path = u.pathname !== '/' ? u.pathname.slice(0, 20) + (u.pathname.length > 20 ? '…' : '') : '';
        return path ? host + path : host;
    } catch (_) {
        return url.length > 40 ? url.slice(0, 37) + '…' : url;
    }
}

/**
 * truncateSnippet(text, maxLen) — Truncate to maxLen with ellipsis, one line for card content.
 */
function truncateSnippet(text, maxLen) {
    if (!text || typeof text !== 'string') return '';
    const t = text.trim();
    if (t.length <= maxLen) return t;
    return t.slice(0, maxLen).trim() + '…';
}

/**
 * renderSearchResults(payload) — Fills the right-side search results widget
 * with Tavily data (query, AI answer, and source cards). Filters junk, truncates
 * content, and shows friendly URL labels so layout stays clean and responsive.
 */
function renderSearchResults(payload) {
    if (!payload) return;
    if (searchResultsQuery) searchResultsQuery.textContent = (payload.query || '').trim() || 'Search';
    if (searchResultsAnswer) searchResultsAnswer.textContent = (payload.answer || '').trim() || '';
    if (!searchResultsList) return;
    searchResultsList.innerHTML = '';
    const results = payload.results || [];
    const maxContentLen = 220;
    for (const r of results) {
        let title = (r.title || '').trim();
        let content = (r.content || '').trim();
        const url = (r.url || '').trim();
        if (isUrlLike(title)) title = friendlyUrlLabel(url) || 'Source';
        if (!title) title = friendlyUrlLabel(url) || 'Source';
        if (isUrlLike(content)) content = '';
        content = truncateSnippet(content, maxContentLen);
        const score = r.score != null ? Math.round((r.score || 0) * 100) : null;
        const card = document.createElement('div');
        card.className = 'search-result-card';
        const urlDisplay = url ? escapeHtml(friendlyUrlLabel(url)) : '';
        const hrefSafe = safeUrlForHref(url);
        const urlMarkup = urlDisplay
            ? (hrefSafe ? `<a href="${hrefSafe}" target="_blank" rel="noopener" class="card-url" title="${escapeAttr(url)}">${urlDisplay}</a>` : `<span class="card-url">${urlDisplay}</span>`)
            : '';
        card.innerHTML = `
            <div class="card-title">${escapeHtml(title)}</div>
            ${content ? `<div class="card-content">${escapeHtml(content)}</div>` : ''}
            ${urlMarkup}
            ${score != null ? `<div class="card-score">Relevance: ${escapeHtml(String(score))}%</div>` : ''}`;
        searchResultsList.appendChild(card);
    }
}

/**
 * safeUrlForHref(url) — Returns URL only if it's http/https; otherwise empty.
 * Prevents XSS via javascript:, data:, or other dangerous protocols.
 */
function safeUrlForHref(url) {
    if (!url || typeof url !== 'string') return '';
    const u = url.trim();
    if (u.startsWith('https://') || u.startsWith('http://')) return escapeAttr(u);
    return '';
}

/**
 * escapeAttr(str) — Escape for HTML attribute (e.g. href, title).
 * Order matters: & first, then ", <, >.
 */
function escapeAttr(str) {
    if (typeof str !== 'string') return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/** Step labels for activity events (left panel). */
const ACTIVITY_STEPS = {
    query_detected:      { step: 1, label: 'Query detected' },
    decision:            { step: 2, label: 'Brain decision' },
    routing:             { step: 3, label: 'Route selected' },
    streaming_started:   { step: 4, label: 'Streaming response' },
    extracting_query:    { step: 0, label: 'Extracting query' },
    searching_web:       { step: 0, label: 'Searching web' },
    search_completed:    { step: 0, label: 'Search completed' },
    context_retrieved:   { step: 0, label: 'Context retrieved' },
    first_chunk:         { step: 5, label: 'Core responded' },
    tool_executed:       { step: 0, label: 'Tool executed' },
};

/**
 * appendActivity(activity) — Appends an activity event to the left panel.
 * Structured with step numbers, icons, and clear hierarchy.
 */
function appendActivity(activity) {
    if (!activityList || !activity) return;
    const item = document.createElement('div');
    item.className = 'activity-item';
    item.setAttribute('data-event', activity.event || '');
    const stepInfo = ACTIVITY_STEPS[activity.event] || { step: 0, label: activity.event || 'Activity', icon: 'dot' };
    let detail = '';
    if (activity.event === 'query_detected') {
        detail = activity.message || '';
    } else if (activity.event === 'decision') {
        const ms = activity.elapsed_ms;
        const timing = ms != null ? ` (Cortex: ${ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(2) + ' s'})` : '';
        detail = `${(activity.query_type || '?').charAt(0).toUpperCase() + (activity.query_type || '').slice(1)} — ${activity.reasoning || ''}${timing}`;
        if (activity.query_type === 'general') item.classList.add('route-general');
        if (activity.query_type === 'realtime') item.classList.add('route-realtime');
    } else if (activity.event === 'routing') {
        detail = `→ ${(activity.route || '?').charAt(0).toUpperCase() + (activity.route || '').slice(1)}`;
        if (activity.route === 'general') item.classList.add('route-general');
        if (activity.route === 'realtime') item.classList.add('route-realtime');
    } else if (activity.event === 'streaming_started') {
        detail = `Generating via ${(activity.route || '?').charAt(0).toUpperCase() + (activity.route || '').slice(1)}`;
        if (activity.route === 'general') item.classList.add('route-general');
        if (activity.route === 'realtime') item.classList.add('route-realtime');
    } else if (activity.event === 'first_chunk') {
        const ms = activity.elapsed_ms;
        detail = ms != null ? `Core responded in ${ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(2) + ' s'}` : 'Response started';
        if (activity.route === 'general') item.classList.add('route-general');
        if (activity.route === 'realtime') item.classList.add('route-realtime');
    } else if (activity.event === 'extracting_query') {
        detail = activity.message || 'Parsing your question for search...';
        item.classList.add('activity-sub');
    } else if (activity.event === 'searching_web') {
        detail = activity.message || (activity.query ? `Query: "${activity.query}"` : 'Scanning Pulse...');
        item.classList.add('activity-sub', 'route-realtime');
    } else if (activity.event === 'search_completed') {
        detail = activity.message || 'Search completed';
        item.classList.add('activity-sub', 'route-realtime');
    } else if (activity.event === 'context_retrieved') {
        detail = activity.message || 'Knowledge base ready';
        item.classList.add('activity-sub', 'route-general');
    } else if (activity.event === 'tool_executed') {
        detail = `${activity.tool || 'Tool'}: ${activity.result || 'Done'}`;
        item.classList.add('activity-sub', 'route-realtime');
    } else {
        detail = activity.message || (typeof activity === 'object' ? JSON.stringify(activity) : String(activity));
    }
    const stepNum = stepInfo.step ? `<span class="activity-step">${stepInfo.step}</span>` : '';
    item.innerHTML = `
        <div class="activity-event">${stepNum}${escapeHtml(stepInfo.label)}</div>
        <div class="activity-detail">${escapeHtml(detail || '')}</div>`;
    const emptyEl = activityList.querySelector('.activity-empty');
    if (emptyEl) emptyEl.style.display = 'none';
    activityList.appendChild(item);
    activityList.scrollTop = activityList.scrollHeight;
}

/**
 * escapeHtml(str) — Escapes & < > " ' for safe insertion into HTML.
 */
function escapeHtml(str) {
    if (typeof str !== 'string') return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

/**
 * hideWelcome() — Removes the welcome screen from the DOM.
 *
 * Called before adding the first message, since the welcome screen
 * should disappear once a conversation begins.
 */
function hideWelcome() {
    const w = document.getElementById('welcome-screen');
    if (w) w.remove();
}

/**
 * addMessage(role, text) — Creates and appends a chat message bubble.
 *
 * @param {string} role - Either 'user' or 'assistant'. Determines
 *                         styling, avatar letter, and label text.
 * @param {string} text - The message content to display.
 * @returns {HTMLDivElement} The inner content element — returned so
 *                           the caller (sendMessage) can update it
 *                           later during streaming.
 *
 * DOM structure created:
 *   <div class="message user|assistant">
 *     <div class="msg-avatar"><svg>...</svg></div>
 *     <div class="msg-body">
 *       <div class="msg-label">Jarvis (General) | You</div>
 *       <div class="msg-content">...text...</div>
 *     </div>
 *   </div>
 */

/**
 * stripActionTags(text) — Remove [ACTION:tool(params)] tags from text.
 * Uses a state-machine instead of regex so ) inside quoted strings is handled.
 */
function stripActionTags(text) {
    let result = '';
    let i = 0;
    const marker = '[ACTION:';
    while (i < text.length) {
        const idx = text.toLowerCase().indexOf(marker.toLowerCase(), i);
        if (idx === -1) { result += text.slice(i); break; }
        result += text.slice(i, idx);
        // Find opening (
        const parenPos = text.indexOf('(', idx + marker.length);
        if (parenPos === -1) { result += text.slice(idx); break; }
        // Walk to matching ) respecting quotes
        let depth = 0, inQuote = null, j = parenPos;
        let foundClose = -1;
        while (j < text.length) {
            const ch = text[j];
            if (inQuote) {
                if (ch === '\\' && j + 1 < text.length) { j += 2; continue; }
                if (ch === inQuote) inQuote = null;
            } else {
                if (ch === '"' || ch === "'") inQuote = ch;
                else if (ch === '(') depth++;
                else if (ch === ')') { depth--; if (depth === 0) { foundClose = j; break; } }
            }
            j++;
        }
        if (foundClose !== -1 && foundClose + 1 < text.length && text[foundClose + 1] === ']') {
            i = foundClose + 2; // Skip the entire tag
        } else {
            result += text[idx]; i = idx + 1; // Not a valid tag, keep character
        }
    }
    // Clean up: collapse 3+ consecutive newlines into 2
    var _multiNL = new RegExp('\\n\\s*\\n\\s*\\n', 'g');
    return result.replace(_multiNL, '\n\n').trim();
}

/* Inline SVG icons for chat avatars (user = person, assistant = bot). */
const AVATAR_ICON_USER = '<svg class="msg-avatar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
const AVATAR_ICON_ASSISTANT = '<svg class="msg-avatar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><circle cx="9" cy="16" r="1" fill="currentColor"/><circle cx="15" cy="16" r="1" fill="currentColor"/></svg>';

function addMessage(role, text) {
    hideWelcome();
    const msg = document.createElement('div');
    msg.className = `message ${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.innerHTML = role === 'assistant' ? AVATAR_ICON_ASSISTANT : AVATAR_ICON_USER;

    const body = document.createElement('div');
    body.className = 'msg-body';

    const label = document.createElement('div');
    label.className = 'msg-label';
    label.textContent = role === 'assistant'
        ? `Jarvis  (${currentMode === 'jarvis' ? 'Jarvis' : currentMode === 'realtime' ? 'Realtime' : 'General'})`
        : 'You';

    const content = document.createElement('div');
    content.className = 'msg-content';
    content.textContent = text;

    body.appendChild(label);
    body.appendChild(content);

    // Copy button for assistant messages
    if (role === 'assistant') {
        const copyBtn = document.createElement('button');
        copyBtn.className = 'msg-copy-btn';
        copyBtn.title = 'Copy to clipboard';
        copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
        copyBtn.addEventListener('click', () => {
            const textEl = content.querySelector('.msg-stream-text');
            const copyText = textEl ? textEl.textContent : content.textContent;
            navigator.clipboard.writeText(copyText).then(() => {
                copyBtn.classList.add('copied');
                copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>';
                setTimeout(() => {
                    copyBtn.classList.remove('copied');
                    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
                }, 2000);
            }).catch(() => {});
        });
        body.appendChild(copyBtn);
    }

    msg.appendChild(avatar);
    msg.appendChild(body);
    chatMessages.appendChild(msg);
    scrollToBottom();
    return content;  // Returned so the streaming logic can update it in real time
}

/**
 * addTypingIndicator() — Shows an animated "..." typing indicator
 * while waiting for the assistant's response to begin streaming.
 *
 * @returns {HTMLDivElement} The content element (containing the dots).
 *
 * This creates a message bubble that looks like the assistant is
 * typing. It's removed once actual content starts arriving.
 * The three <span> elements inside .typing-dots are animated via CSS
 * to create the bouncing dots effect.
 */
function addTypingIndicator() {
    hideWelcome();
    const msg = document.createElement('div');
    msg.className = 'message assistant';
    msg.id = 'typing-msg';               // ID so we can find and remove it later

    const avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.innerHTML = AVATAR_ICON_ASSISTANT;

    const body = document.createElement('div');
    body.className = 'msg-body';

    const label = document.createElement('div');
    label.className = 'msg-label';
    label.textContent = `Jarvis  (${currentMode === 'jarvis' ? 'Jarvis' : currentMode === 'realtime' ? 'Realtime' : 'General'})`;

    const content = document.createElement('div');
    content.className = 'msg-content';
    content.innerHTML = '<span class="msg-stream-text">...</span>';

    body.appendChild(label);
    body.appendChild(content);
    msg.appendChild(avatar);
    msg.appendChild(body);
    chatMessages.appendChild(msg);
    scrollToBottom();
    return content;
}

/**
 * removeTypingIndicator() — Removes the typing indicator from the DOM.
 *
 * Called when:
 *   - The first token of the response arrives (replaced by real content).
 *   - An error occurs (replaced by an error message).
 */
function removeTypingIndicator() {
    const t = document.getElementById('typing-msg');
    if (t) t.remove();
}

/**
 * scrollToBottom() — Scrolls the chat container to show the latest message.
 *
 * Uses requestAnimationFrame so the scroll runs after the browser has
 * laid out newly added content (typing indicator, "Thinking...", or
 * streamed chunks). Without this, scroll can happen before layout and
 * the user would have to scroll manually to see new content.
 */
function scrollToBottom() {
    requestAnimationFrame(() => {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    });
}

/* ================================================================
   SEND MESSAGE + SSE STREAMING
   ================================================================

   HOW SSE (Server-Sent Events) STREAMING WORKS — EXPLAINED FOR LEARNERS
   ----------------------------------------------------------------------
   Instead of waiting for the entire AI response to generate (which
   could take seconds), we use SSE streaming to receive the response
   token-by-token as it's generated. This creates the "typing" effect.

   STANDARD SSE FORMAT:
   The server sends a stream of lines like:
     data: {"chunk": "Hello"}
     data: {"chunk": " there"}
     data: {"chunk": "!"}
     data: {"done": true}

   Each line starts with "data: " followed by a JSON payload. Lines
   are separated by newlines ("\n"). An empty line separates events.

   HOW WE READ THE STREAM:
   1. We POST the user's message to the backend.
   2. The server responds with Content-Type: text/event-stream.
   3. We use res.body.getReader() to read the response body as a
      stream of raw bytes (Uint8Array chunks).
   4. We decode each chunk to text and append it to an SSE buffer.
   5. We split the buffer by newlines and process each complete line.
   6. Lines starting with "data: " are parsed as JSON.
   7. Each JSON payload may contain:
      - chunk: a piece of the text response (appended to the UI)
      - audio: a base64 MP3 segment (enqueued for TTS playback)
      - session_id: the conversation ID (saved for future messages)
      - error: an error message from the server
      - done: true when the response is complete

   WHY NOT USE EventSource?
   The native EventSource API only supports GET requests. We need POST
   (to send the message body), so we use fetch() + manual SSE parsing.

   THE SSE BUFFER:
   Network chunks don't align with SSE line boundaries — one chunk
   might contain half a line, or multiple lines. The sseBuffer variable
   accumulates raw text. We split by '\n', process all complete lines,
   and keep the last (potentially incomplete) line in the buffer for
   the next iteration.

   ================================================================ */

/**
 * showJarvisGoodbye() — Called when JARVIS is shutting down.
 * Shows a goodbye message, fades the orb, disables input.
 */
function showJarvisGoodbye() {
    // Don't add a message -- the LLM already said "Goodbye, sir." before the shutdown event

    // Disable input
    if (messageInput) messageInput.disabled = true;
    if (sendBtn) sendBtn.disabled = true;

    // Fade the orb
    if (orbContainer) {
        orbContainer.style.transition = 'opacity 2s ease-out';
        orbContainer.style.opacity = '0.3';
    }

    // Show offline message after 3 seconds
    setTimeout(() => {
        const chatMessages = document.getElementById('chat-messages');
        if (chatMessages) {
            const offlineDiv = document.createElement('div');
            offlineDiv.className = 'message assistant';
            offlineDiv.style.textAlign = 'center';
            offlineDiv.style.opacity = '0.5';
            offlineDiv.innerHTML = '<em>JARVIS is offline.</em>';
            chatMessages.appendChild(offlineDiv);
            scrollToBottom();
        }
    }, 3000);
}

/**
 * sendMessage(textOverride) — Sends a user message and streams the AI response.
 *
 * AUDIO WORKFLOW (minimizes waiting):
 *   1. Pre-starter: Play random cached audio on dedicated PreStarterPlayer (immune to reset).
 *   2. Main: Stream from chatbot; when first real chunk arrives, reset() and main TTS plays.
 */
async function sendMessage(textOverride) {
    // Step 1: Get the message text, trimming whitespace
    const text = (textOverride || messageInput.value).trim();
    if (!text || isStreaming) return;  // Ignore empty messages or if already streaming

    // Step 2: Clear the input field immediately (responsive UX)
    messageInput.value = '';
    autoResizeInput();
    charCount.textContent = '';

    // Step 3: Display the user's message and show typing indicator
    addMessage('user', text);
    addTypingIndicator();

    // Step 4: Lock the UI to prevent double-sending
    isStreaming = true;
    if (sendBtn) sendBtn.disabled = true;
    if (messageInput) messageInput.disabled = true;
    if (orbContainer) orbContainer.classList.add('active');

    // Step 5: Reset TTS for this new response and unlock audio (iOS)
    if (ttsPlayer) { ttsPlayer.reset(); ttsPlayer.unlock(); }

    // Step 6: Choose the endpoint based on the current mode
    const endpoint = currentMode === 'jarvis'
        ? '/chat/jarvis/stream'
        : currentMode === 'realtime'
            ? '/chat/realtime/stream'
            : '/chat/stream';

    // Clear activity panel (query_detected only — no duplicate user message)
    if (activityList) {
        activityList.innerHTML = '<div class="activity-empty" id="activity-empty">Processing...</div>';
        if (activityToggle) activityToggle.style.display = '';
        if (activityPanel && settings.autoOpenActivity) { activityPanel.classList.add('open'); updatePanelOverlay(); }
    }

    let firstChunkReceived = false;
    let timeoutId = null;
    const controller = new AbortController();

    try {
        // ─── Pre-starter + Main stream ───
        // 1. Pre-starter: play immediately on dedicated audio element (immune to ttsPlayer.reset)
        if (ttsPlayer?.enabled && settings.thinkingSounds && preStarterPlayer) {
            preStarterPlayer.play(() => {});
        }
        // 2. Main: stream from chatbot (general or realtime)
        timeoutId = setTimeout(() => controller.abort(), 300000);
        const res = await fetch(`${API}${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: text,                                 // The user's message
                session_id: sessionId,                         // null on first message; UUID after that
                tts: !!(ttsPlayer && ttsPlayer.enabled)        // Tell the backend whether to generate audio
            }),
            signal: controller.signal,
        });

        // Handle HTTP errors (4xx, 5xx)
        if (!res.ok) {
            const err = await res.json().catch(() => null);
            throw new Error(err?.detail || `HTTP ${res.status}`);
        }

        // Step 8: Replace the typing indicator with an empty assistant message
        removeTypingIndicator();
        const contentEl = addMessage('assistant', '');
        contentEl.innerHTML = '<span class="msg-stream-text">...</span>';
        scrollToBottom();   // Scroll so placeholder is visible without manual scroll

        // Set up the stream reader and SSE parser
        if (!res.body) throw new Error('No response body');
        const reader = res.body.getReader();       // ReadableStream reader for the response body
        const decoder = new TextDecoder();          // Converts raw bytes (Uint8Array) to strings
        let sseBuffer = '';                         // Accumulates partial SSE lines between chunks
        let fullResponse = '';                      // The complete assistant response text so far
        let cursorEl = null;                        // The blinking "|" cursor shown during streaming
        let emotionTag = 'neutral';                 // Emotion detected from [EMOTION:tag] prefix

        // Step 9: Read the stream in a loop until it's done
        let streamDone = false;
        while (!streamDone) {
            const { done, value } = await reader.read();
            if (done) break;  // Stream has ended

            // Decode the bytes and add to our SSE buffer
            sseBuffer += decoder.decode(value, { stream: true });

            // Split by newlines to get individual SSE lines
            const lines = sseBuffer.split('\n');

            // The last element might be an incomplete line — keep it in the buffer
            sseBuffer = lines.pop();

            // Process each complete line
            for (const line of lines) {
                // SSE lines that don't start with "data: " are empty lines or comments — skip them
                if (!line.startsWith('data: ')) continue;
                try {
                    // Parse the JSON payload (everything after "data: ")
                    const data = JSON.parse(line.slice(6));

                    // Save the session ID if the server sends one
                    if (data.session_id) sessionId = data.session_id;

                    // ACTIVITY — Jarvis flow (query detected, decision, routing): show in left panel
                    if (data.activity) {
                        appendActivity(data.activity);
                        if (activityToggle) activityToggle.style.display = '';
                        if (activityPanel && settings.autoOpenActivity) { activityPanel.classList.add('open'); updatePanelOverlay(); }
                    }

                    // SEARCH RESULTS — Tavily data (realtime only): show in right-side widget and reveal toggle
                    if (data.search_results) {
                        renderSearchResults(data.search_results);
                        if (searchResultsToggle) searchResultsToggle.style.display = '';
                        if (searchResultsWidget && settings.autoOpenSearchResults) { searchResultsWidget.classList.add('open'); updatePanelOverlay(); }
                    }

                    // TEXT CHUNK — Append to the displayed response (chunk can be "" in some streams)
                    if ('chunk' in data) {
                        const chunkText = data.chunk || '';
                        // Only treat as "main started" when we get actual content — the initial event
                        // has chunk: "" for session_id; that would wrongly reset
                        if (chunkText && !firstChunkReceived) {
                            firstChunkReceived = true;
                            if (ttsPlayer) ttsPlayer.reset();   // Stop pre-starter, play main immediately
                        }
                        fullResponse += chunkText;
                        // Strip [EMOTION:tag] prefix (sent by LLM for UI emotion feedback)
                        let displayText = fullResponse;
                        const emotionMatch = displayText.match(/^\[EMOTION:(\w+)\]\n?/);
                        if (emotionMatch) {
                            emotionTag = emotionMatch[1];
                            displayText = displayText.slice(emotionMatch[0].length);
                        }
                        // Strip [ACTION:tool(params)] tags using state-machine (handles ")" inside quotes)
                        displayText = stripActionTags(displayText);
                        // Buffer: if text ends with an incomplete [ACTION: prefix, hold it back
                        const partialIdx = displayText.lastIndexOf('[ACTION:');
                        if (partialIdx !== -1 && displayText.indexOf(']', partialIdx) === -1) {
                            displayText = displayText.slice(0, partialIdx);
                        }
                        const textSpan = contentEl.querySelector('.msg-stream-text');
                        if (textSpan) {
                            textSpan.textContent = displayText;
                            textSpan.classList.remove('stream-placeholder');
                        }

                        // Add a blinking cursor at the end (created once, on the first chunk)
                        if (!cursorEl) {
                            cursorEl = document.createElement('span');
                            cursorEl.className = 'stream-cursor';
                            cursorEl.textContent = '|';
                            contentEl.appendChild(cursorEl);
                        }
                        scrollToBottom();
                    }

                    // AUDIO CHUNK — Enqueue for TTS playback
                    if (data.audio && ttsPlayer) {
                        ttsPlayer.enqueue(data.audio);
                    }

                    // ERROR — The server reported an error in the stream
                    if (data.error) throw new Error(data.error);

                    // DONE — The server signals that the response is complete
                    if (data.done) { streamDone = true; break; }

                    // SHUTDOWN — JARVIS is shutting down (exit_jarvis tool was triggered)
                    if (data.shutdown) {
                        streamDone = true;
                        showJarvisGoodbye();
                        break;
                    }
                } catch (parseErr) {
                    // Ignore JSON parse errors (e.g., partial lines) but re-throw real errors
                    if (parseErr.message && !parseErr.message.includes('JSON'))
                        throw parseErr;
                }
            }
            if (streamDone) break;
        }

        // Step 10: Clean up — remove the blinking cursor
        if (cursorEl) cursorEl.remove();

        // If the server sent nothing, show a placeholder
        const textSpan = contentEl.querySelector('.msg-stream-text');
        if (textSpan && !fullResponse) textSpan.textContent = '(No response)';

    } catch (err) {
        clearTimeout(timeoutId);
        removeTypingIndicator();
        const msg = err.name === 'AbortError' ? 'Request timed out. Please try again.' : `Something went wrong: ${err.message}`;
        addMessage('assistant', msg);
        showToast(msg);
    } finally {
        clearTimeout(timeoutId);
        isStreaming = false;
        if (sendBtn) sendBtn.disabled = false;
        if (messageInput) messageInput.disabled = false;
        if (orbContainer) orbContainer.classList.remove('active');
        maybeRestartListening();   // Auto-restart mic when stream ends (TTS may still be playing)
    }
}

/* ================================================================
   BOOT — Application Entry Point
   ================================================================
   DOMContentLoaded fires when the HTML document has been fully parsed
   (but before images/stylesheets finish loading). This is the ideal
   time to initialize our app because all DOM elements are available.
   ================================================================ */
document.addEventListener('DOMContentLoaded', init);

/* ================================================================
   CHAT HISTORY SIDEBAR
   ================================================================ */

async function toggleHistoryPanel() {
    const panel = document.getElementById('history-panel');
    if (!panel) return;
    const isOpen = panel.classList.contains('open');
    if (isOpen) {
        panel.classList.remove('open');
    } else {
        panel.classList.add('open');
        await loadHistoryList();
    }
    updatePanelOverlay();
}

async function loadHistoryList() {
    const list = document.getElementById('history-list');
    if (!list) return;
    list.innerHTML = '<div class="history-loading">Loading...</div>';
    try {
        const r = await fetch(`${API}/chat/sessions`);
        const d = await r.json();
        list.innerHTML = '';
        if (!d.sessions || d.sessions.length === 0) {
            list.innerHTML = '<div class="history-empty">No saved conversations yet.</div>';
            return;
        }
        for (const s of d.sessions) {
            const item = document.createElement('div');
            item.className = 'history-item';
            const date = new Date(s.modified * 1000);
            const dateStr = date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
            item.innerHTML = `
                <div class="history-item-preview">${escapeHtml(s.preview)}</div>
                <div class="history-item-meta">
                    <span>${s.message_count} messages</span>
                    <span>${dateStr}</span>
                </div>
                <div class="history-item-actions">
                    <button class="history-load-btn" title="Load this chat">Load</button>
                    <button class="history-export-btn" title="Export">Export</button>
                    <button class="history-delete-btn" title="Delete">Delete</button>
                </div>`;
            // Load button
            item.querySelector('.history-load-btn').addEventListener('click', async () => {
                sessionId = s.session_id;
                if (chatMessages) chatMessages.innerHTML = '';
                try {
                    const hr = await fetch(`${API}/chat/history/${s.session_id}`);
                    const hd = await hr.json();
                    for (const m of (hd.messages || [])) {
                        addMessage(m.role, m.content);
                    }
                } catch (e) { showToast('Failed to load chat', 4000); }
                const panel = document.getElementById('history-panel');
                if (panel) panel.classList.remove('open');
                updatePanelOverlay();
            });
            // Export button
            item.querySelector('.history-export-btn').addEventListener('click', () => {
                window.open(`${API}/chat/export/${s.session_id}?format=md`, '_blank');
            });
            // Delete button
            item.querySelector('.history-delete-btn').addEventListener('click', async (e) => {
                e.stopPropagation();
                if (!confirm('Delete this conversation?')) return;
                try {
                    await fetch(`${API}/chat/session/${s.session_id}`, { method: 'DELETE' });
                    item.remove();
                    showToast('Conversation deleted', 3000);
                    if (sessionId === s.session_id) newChat();
                } catch (err) { showToast('Failed to delete', 4000); }
            });
            list.appendChild(item);
        }
    } catch (e) {
        list.innerHTML = '<div class="history-empty">Failed to load history.</div>';
    }
}

/* ================================================================
   THEME TOGGLE (Dark / Light)
   ================================================================ */
function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('jarvis_theme', next);
    const btn = document.getElementById('theme-btn');
    if (btn) btn.title = next === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
}

// Apply saved theme on load
(function() {
    const saved = localStorage.getItem('jarvis_theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
})();
