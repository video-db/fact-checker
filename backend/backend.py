import os
import sys
import logging
import threading
import queue
import asyncio
import traceback
import time
import json
import secrets
import hmac
from datetime import datetime, timezone

from flask import Flask, request, jsonify, Response
from pycloudflared import try_cloudflare
import videodb
from videodb._constants import RTStreamChannelType

from config import (
    VIDEO_DB_API_KEY,
    GEMINI_API_KEY,
    PORT,
    FACT_CHECK_INTERVAL,
    MIN_WORDS_FOR_CHECK,
    CONFIDENCE_THRESHOLD,
    LOG_DIR,
)
from pipeline import run_pipeline
from pipeline.verifier import Verifier
from pipeline.alert_manager import AlertManager

if not VIDEO_DB_API_KEY:
    print("[ERROR] VIDEO_DB_API_KEY environment variable not set")
    sys.exit(1)

if not GEMINI_API_KEY:
    print("[ERROR] GEMINI_API_KEY environment variable not set")
    sys.exit(1)


# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fact-checker")

os.makedirs(LOG_DIR, exist_ok=True)

# Flask App
app = Flask(__name__)

# Shared State
conn = None
public_url = None
verifier = None
alert_manager = None

# Thread-safe transcript buffer with deduplication
transcript_buffer = []
_recent_transcripts = []  # last N texts for dedup across WS + webhook
_DEDUP_WINDOW = 20
buffer_lock = threading.Lock()

# Sliding context window carried between cycles
pipeline_context = ""
context_lock = threading.Lock()

# Session-level statistics
session_stats = {
    "total_notes": 0,
    "verified": 0,
    "misleading": 0,
    "needs_context": 0,
    "alerted": 0,
    "suppressed_low_confidence": 0,
    "suppressed_duplicate": 0,
    "chunks_analyzed": 0,
}
stats_lock = threading.Lock()

# SSE: event queue for pushing alerts to connected clients
_sse_event_id = 0
_sse_events = []  # list of (id, json_string)
_sse_events_lock = threading.Lock()
_sse_condition = threading.Condition()

# Last transcript text for the stats endpoint
_last_transcript = ""
_last_transcript_lock = threading.Lock()

# Circuit breaker state for Gemini API failures
_consecutive_failures = 0
_current_chunk_retries = 0
_circuit_open_until = 0.0  # timestamp
MAX_CHUNK_RETRIES = 3
MAX_CONSECUTIVE_FAILURES = 5
CIRCUIT_OPEN_DURATION = 60

# Session generation counter for atomic reset detection
_session_generation = 0
_session_gen_lock = threading.Lock()

# Webhook callback authentication
_callback_secret = None

# Callback rate limiting (in-memory)
_callback_counts = {}  # ip -> (count, window_start)
CALLBACK_RATE_LIMIT = 60  # max requests per minute per IP


def _buffer_transcript(text):
    """Append text to the transcript buffer, skipping duplicates.

    Must be called while holding ``buffer_lock`` — this protects both
    ``transcript_buffer`` and ``_recent_transcripts``.
    """
    global _last_transcript
    assert buffer_lock.locked(), "_buffer_transcript called without buffer_lock"
    if text in _recent_transcripts:
        return
    _recent_transcripts.append(text)
    if len(_recent_transcripts) > _DEDUP_WINDOW:
        _recent_transcripts.pop(0)
    transcript_buffer.append(text)
    with _last_transcript_lock:
        _last_transcript = text


_SSE_MAX_EVENTS = 200  # keep only the most recent events in memory


def _push_sse_event(all_notes):
    """Push a fact-check result as an SSE event."""
    global _sse_event_id
    with _sse_events_lock:
        _sse_event_id += 1
        event_id = _sse_event_id
        payload = json.dumps({"id": event_id, "all_notes": all_notes})
        _sse_events.append((event_id, payload))
        # Prune old events to prevent unbounded memory growth
        if len(_sse_events) > _SSE_MAX_EVENTS:
            del _sse_events[: len(_sse_events) - _SSE_MAX_EVENTS]
    with _sse_condition:
        _sse_condition.notify_all()


# File Logging
def log_to_file(entry):
    """Write a formatted JSON entry to a new log file."""
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    filename = os.path.join(LOG_DIR, f"fact_check_{timestamp}.json")
    try:
        with open(filename, "w") as f:
            json.dump(entry, f, indent=4)
        print(f"[LOG] Saved to {filename}")
    except OSError as e:
        logger.error("Failed to write log entry: %s", e)


def _check_rate_limit(ip):
    """Return True if the request is within rate limits, False otherwise."""
    now = time.time()
    count, start = _callback_counts.get(ip, (0, now))
    if now - start > 60:
        _callback_counts[ip] = (1, now)
        return True
    if count >= CALLBACK_RATE_LIMIT:
        return False
    _callback_counts[ip] = (count + 1, start)
    return True


def log_notes(all_notes, transcript_chunk, context_used):
    """Write fact-check results as a structured log entry."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "fact_check",
        "transcript_chunk": transcript_chunk,
        "context_used": context_used,
        "notes": all_notes,
        "summary": {
            "total": len(all_notes),
            "alerted": sum(1 for n in all_notes if n.get("alerted")),
            "verified": sum(1 for n in all_notes if n["label"] == "verified"),
            "misleading": sum(1 for n in all_notes if n["label"] == "misleading"),
            "needs_context": sum(1 for n in all_notes if n["label"] == "needs_context"),
        },
    }
    log_to_file(entry)


# Terminal Display
def display_notes(alerts):
    """Print community-notes style results to the terminal."""
    if not alerts:
        return

    print("\n" + "=" * 60)
    print(f"  COMMUNITY NOTES  ({len(alerts)} note(s) from latest check)")
    print("=" * 60)

    for note in alerts:
        label = note["label"].upper()
        print(f'\n  [{label}] "{note["claim"]}"')
        if note.get("note"):
            # Wrap long notes
            note_text = note["note"]
            print(f"  Note: {note_text}")
        if note.get("sources"):
            print(f"  Sources: {', '.join(note['sources'])}")
        print(f"  Confidence: {note['confidence']}")

    print("\n" + "-" * 60)

    # Update and display running stats
    with stats_lock:
        print(
            f"  Session: {session_stats['total_notes']} notes | "
            f"Verified: {session_stats['verified']} | "
            f"Misleading: {session_stats['misleading']} | "
            f"Needs Context: {session_stats['needs_context']}"
        )
    print("-" * 60)
    sys.stdout.flush()


def update_stats(all_notes, alerts):
    """Update session statistics from a pipeline run."""
    from pipeline.alert_manager import _meets_confidence

    with stats_lock:
        session_stats["chunks_analyzed"] += 1
        for note in all_notes:
            session_stats["total_notes"] += 1
            label = note["label"]
            if label in session_stats:
                session_stats[label] += 1
            if note.get("alerted"):
                session_stats["alerted"] += 1
            else:
                if not _meets_confidence(note.get("confidence"), CONFIDENCE_THRESHOLD):
                    session_stats["suppressed_low_confidence"] += 1
                else:
                    session_stats["suppressed_duplicate"] += 1


# Fact-Check Runner (background thread)
def run_fact_check_loop():
    """Periodically drain the transcript buffer and run the pipeline."""
    global pipeline_context, _consecutive_failures, _current_chunk_retries, _circuit_open_until

    logger.info(
        "Fact-check loop started (interval=%ds, min_words=%d)",
        FACT_CHECK_INTERVAL,
        MIN_WORDS_FOR_CHECK,
    )

    while True:
        time.sleep(FACT_CHECK_INTERVAL)

        # Circuit breaker: skip pipeline calls while circuit is open
        if _circuit_open_until and time.time() < _circuit_open_until:
            logger.debug("Circuit open — skipping pipeline call")
            continue

        # Capture session generation before draining buffer
        with _session_gen_lock:
            gen_before = _session_generation

        # Drain the buffer
        with buffer_lock:
            if not transcript_buffer:
                continue
            chunk = " ".join(transcript_buffer)
            transcript_buffer.clear()

        word_count = len(chunk.split())
        if word_count < MIN_WORDS_FOR_CHECK:
            logger.debug(
                "Chunk too short (%d words), carrying over to next cycle",
                word_count,
            )
            with buffer_lock:
                transcript_buffer.insert(0, chunk)
            continue

        logger.info("Analyzing chunk (%d words)...", word_count)
        print(f"\n[ANALYZING] Processing {word_count} words of transcript...")

        with context_lock:
            ctx = pipeline_context

        try:
            alerts, all_notes, new_context = run_pipeline(
                chunk, ctx, verifier, alert_manager
            )
        except Exception as e:
            logger.error("Pipeline error: %s", e)
            print(f"[ERROR] Pipeline failed: {e}")

            _consecutive_failures += 1
            _current_chunk_retries += 1

            if _current_chunk_retries >= MAX_CHUNK_RETRIES:
                # Discard this chunk after too many retries
                logger.warning(
                    "Chunk discarded after %d retries", _current_chunk_retries
                )
                log_to_file({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "discarded_chunk",
                    "chunk": chunk[:500],
                    "retries": _current_chunk_retries,
                    "error": str(e),
                })
                _current_chunk_retries = 0
            else:
                # Re-insert chunk for retry
                with buffer_lock:
                    transcript_buffer.insert(0, chunk)

            if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                _circuit_open_until = time.time() + CIRCUIT_OPEN_DURATION
                logger.warning(
                    "[CIRCUIT] Open — pausing pipeline for %ds", CIRCUIT_OPEN_DURATION
                )

            continue

        # Success: reset failure state
        _consecutive_failures = 0
        _current_chunk_retries = 0
        _circuit_open_until = 0.0

        # Check if session changed during pipeline run (stale result)
        with _session_gen_lock:
            if _session_generation != gen_before:
                logger.info("Session changed during pipeline run, discarding results")
                continue

        with context_lock:
            pipeline_context = new_context

        update_stats(all_notes, alerts)
        display_notes(alerts)
        log_notes(all_notes, chunk, ctx)

        # Push alerted notes to SSE clients
        if alerts:
            _push_sse_event(alerts)


# WebSocket Listener
def start_ws_listener(result_queue, name="FactCheckerWS"):
    """Start a background thread that listens for real-time transcript events."""

    def run():
        async def listen():
            try:
                logger.info("[%s] Connecting to WebSocket...", name)
                ws_wrapper = conn.connect_websocket()
                ws = await ws_wrapper.connect()
                ws_id = ws.connection_id
                logger.info("[%s] Connected (ID: %s)", name, ws_id)

                # Send the connection ID back so the caller can bind streams
                result_queue.put(ws_id)

                async for msg in ws.receive():
                    channel = msg.get("channel")
                    data = msg.get("data", {})

                    if channel == "transcript":
                        text = data.get("text", "").strip()
                        is_final = data.get("is_final", False)
                        if text and is_final:
                            with buffer_lock:
                                _buffer_transcript(text)

            except Exception as e:
                logger.error("[%s] WebSocket error: %s", name, e)
                traceback.print_exc()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(listen())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# Flask Routes
@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "tunnel": public_url})


@app.route("/events", methods=["GET"])
def events():
    """SSE endpoint that streams fact-check alerts to connected clients."""
    try:
        last_id = int(request.headers.get("Last-Event-ID", 0))
    except (ValueError, TypeError):
        last_id = 0

    def stream():
        nonlocal last_id
        # First, send any events the client missed
        with _sse_events_lock:
            for eid, payload in _sse_events:
                if eid > last_id:
                    yield f"id: {eid}\ndata: {payload}\n\n"
                    last_id = eid

        # Then wait for new events
        while True:
            with _sse_condition:
                _sse_condition.wait(timeout=15)

            with _sse_events_lock:
                for eid, payload in _sse_events:
                    if eid > last_id:
                        yield f"id: {eid}\ndata: {payload}\n\n"
                        last_id = eid

            # Send a heartbeat comment to keep the connection alive
            yield ": heartbeat\n\n"

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/stats", methods=["GET"])
def stats():
    """Return current session statistics for the Electron frontend."""
    with stats_lock:
        data = dict(session_stats)
    with _last_transcript_lock:
        data["last_transcript"] = _last_transcript
    return jsonify(data)


@app.route("/init-session", methods=["POST"])
def init_session():
    """Create a VideoDB capture session and return credentials."""
    global pipeline_context, _last_transcript, _sse_event_id, _callback_secret
    global _consecutive_failures, _current_chunk_retries, _circuit_open_until
    global _session_generation

    # Increment session generation FIRST so the fact-check loop detects the reset
    with _session_gen_lock:
        _session_generation += 1

    # Reset all session state so stale data doesn't leak across sessions
    with buffer_lock:
        transcript_buffer.clear()
        _recent_transcripts.clear()
    with _last_transcript_lock:
        _last_transcript = ""
    with context_lock:
        pipeline_context = ""
    with stats_lock:
        for key in session_stats:
            session_stats[key] = 0
    alert_manager.reset()
    with _sse_events_lock:
        _sse_events.clear()
        _sse_event_id = 0

    # Reset circuit breaker state for new session
    _consecutive_failures = 0
    _current_chunk_retries = 0
    _circuit_open_until = 0.0

    # Generate a new callback secret for this session
    _callback_secret = secrets.token_urlsafe(32)

    logger.info("Session state reset for new session")

    try:
        callback_url = f"{public_url}/callback?token={_callback_secret}"
        logger.info("Creating session with callback: %s", callback_url)

        session = conn.create_capture_session(
            end_user_id="user_fact_checker",
            collection_id="default",
            callback_url=callback_url,
            metadata={"app": "fact-checker"},
        )

        token = conn.generate_client_token()

        return jsonify({
            "session_id": session.id,
            "token": token,
            "callback_url": callback_url,
        })
    except Exception as e:
        logger.error("Error creating session: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/callback", methods=["POST"])
def callback():
    """Handle VideoDB capture session lifecycle webhooks."""
    # Validate callback token (passed as query parameter because VideoDB is
    # the caller and doesn't support custom HMAC signing headers).
    token = request.args.get("token", "")
    if not _callback_secret or not hmac.compare_digest(token, _callback_secret):
        logger.warning("Unauthorized callback attempt from %s", request.remote_addr)
        return jsonify({"error": "unauthorized"}), 403

    # Rate limiting
    if not _check_rate_limit(request.remote_addr):
        logger.warning("Rate limit exceeded for %s", request.remote_addr)
        return jsonify({"error": "rate limit exceeded"}), 429

    data = request.json
    event = data.get("event")

    # Transcripts can arrive as webhook callbacks with "type" instead of "event"
    if event is None:
        cb_type = data.get("type")
        if cb_type == "transcript":
            text = data.get("text", "").strip()
            is_final = data.get("is_final", False)
            if text and is_final:
                print(f"  [TRANSCRIPT] {text}")
                with buffer_lock:
                    _buffer_transcript(text)
        return jsonify({"received": True})

    logger.info("[WEBHOOK] Event: %s", event)

    if event == "capture_session.active":
        cap_id = data.get("capture_session_id")
        logger.info("Capture session active: %s", cap_id)

        try:
            cap = conn.get_capture_session(cap_id)

            system_audios = cap.get_rtstream(RTStreamChannelType.system_audio)
            mics = cap.get_rtstream(RTStreamChannelType.mic)

            logger.info(
                "Streams found -- System Audio: %d, Mics: %d",
                len(system_audios),
                len(mics),
            )

            # Prefer system audio (captures video playback / meeting audio)
            audio_stream = None
            if system_audios:
                audio_stream = system_audios[0]
                logger.info("Using system audio stream: %s", audio_stream.id)
            elif mics:
                audio_stream = mics[0]
                logger.info("Falling back to microphone stream: %s", audio_stream.id)

            if audio_stream:
                q = queue.Queue()
                start_ws_listener(q, name="FactCheckerWS")
                ws_id = q.get(timeout=10)

                audio_stream.start_transcript(ws_connection_id=ws_id)
                logger.info("Transcription started on WebSocket: %s", ws_id)

                print("\n" + "=" * 60)
                print("  FACT CHECKER ACTIVE")
                print("  Listening for audio and checking facts in real-time...")
                print(f"  Check interval: {FACT_CHECK_INTERVAL}s")
                print(f"  Log directory: {LOG_DIR}")
                print("=" * 60 + "\n")
            else:
                logger.warning("No audio streams available for fact-checking")

        except Exception as e:
            logger.error("Error starting fact-check pipeline: %s", e)
            traceback.print_exc()

    elif event == "capture_session.stopping":
        logger.info("Session stopping...")

    elif event == "capture_session.stopped" or event == "capture_session.failed":
        if event == "capture_session.failed":
            logger.warning("Session failed, but attempting to save remaining logs...")
        else:
            logger.info("Session stopped.")

        # Flush any remaining transcript in the buffer
        remaining = None
        with buffer_lock:
            if transcript_buffer:
                remaining = " ".join(transcript_buffer)
                transcript_buffer.clear()

        if remaining and len(remaining.split()) >= MIN_WORDS_FOR_CHECK:
            logger.info("Checking remaining buffered transcript...")
            with context_lock:
                ctx = pipeline_context

            alerts, all_notes, _ = run_pipeline(
                remaining, ctx, verifier, alert_manager
            )
            update_stats(all_notes, alerts)
            display_notes(alerts)
            log_notes(all_notes, remaining, ctx)
            if alerts:
                _push_sse_event(alerts)

        # Log final session summary
        with stats_lock:
            summary_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "session_summary",
                "stats": dict(session_stats),
            }
            log_to_file(summary_entry)

            print("\n" + "=" * 60)
            print("  SESSION SUMMARY")
            print(f"  Total notes: {session_stats['total_notes']}")
            print(f"  Verified: {session_stats['verified']}")
            print(f"  Misleading: {session_stats['misleading']}")
            print(f"  Needs Context: {session_stats['needs_context']}")
            print(f"  Alerted: {session_stats['alerted']}")
            print(f"  Suppressed (low confidence): {session_stats['suppressed_low_confidence']}")
            print(f"  Suppressed (duplicate): {session_stats['suppressed_duplicate']}")
            print(f"  Chunks analyzed: {session_stats['chunks_analyzed']}")
            print(f"  Full logs in: {LOG_DIR}")
            print("=" * 60)

        # Reset alert manager for next session
        alert_manager.reset()

    elif event == "capture_session.exported":
        video_id = data.get("data", {}).get("exported_video_id")
        logger.info("Recording exported. Video ID: %s", video_id)
        print(f"\n[EXPORTED] Video ID: {video_id}")
        print(f"  View at: https://console.videodb.io/player?video={video_id}")

    return jsonify({"received": True})


# Initialization
def init_app():
    """Initialize VideoDB connection, pipeline components, tunnel, and background tasks."""
    global conn, public_url, verifier, alert_manager

    print("=" * 60)
    print("  FACT CHECKER - Real-time Fact Checking")
    print("  Powered by VideoDB Capture + Gemini")
    print("=" * 60)

    # 1. Connect to VideoDB
    print("\n[INIT] Connecting to VideoDB...")
    conn = videodb.connect(api_key=VIDEO_DB_API_KEY)
    print("[INIT] VideoDB connected.")

    # 2. Initialize pipeline components
    print("[INIT] Initializing Verifier...")
    verifier = Verifier(api_key=GEMINI_API_KEY)
    print("[INIT] Verifier ready.")

    print("[INIT] Initializing AlertManager...")
    alert_manager = AlertManager()
    print("[INIT] AlertManager ready.")

    # 3. Start Cloudflare tunnel for webhooks
    print(f"[INIT] Starting Cloudflare tunnel on port {PORT}...")
    tunnel = try_cloudflare(port=PORT)
    public_url = tunnel.tunnel
    print(f"[INIT] Tunnel active: {public_url}")

    # 4. Start the background fact-check loop
    fact_thread = threading.Thread(target=run_fact_check_loop, daemon=True)
    fact_thread.start()
    print("[INIT] Fact-check loop started.")

    print(f"[INIT] Log directory: {LOG_DIR}")
    print(f"\n[READY] Backend running on http://localhost:{PORT}")
    print("[READY] Now start the client:  python client.py\n")


if __name__ == "__main__":
    init_app()
    app.run(port=PORT, threaded=True)
