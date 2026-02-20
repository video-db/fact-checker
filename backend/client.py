import asyncio
import logging
import os
import requests
import signal
import subprocess
import sys
import time
import webbrowser

from videodb.capture import CaptureClient

from config import PORT

BACKEND_URL = f"http://localhost:{PORT}"
OPEN_DELAY_SECONDS = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fact-checker-client")


def validate_youtube_url(url):
    """Check if the URL looks like a YouTube link."""
    return "youtube.com" in url or "youtu.be" in url


def validate_meet_url(url):
    """Check if the URL looks like a Google Meet link."""
    return "meet.google.com" in url


def validate_local_file(path):
    """Check if the local file path exists."""
    return os.path.exists(path)


def open_content(source_type, target):
    """Open the selected content source and wait for it to load."""
    if source_type in ("youtube", "meet", "stream"):
        print(f"[OPEN] Opening {target} in your browser...")
        webbrowser.open(target)
    elif source_type == "local":
        print(f"[OPEN] Opening {target} with default player...")
        subprocess.run(["open", target])
    print(f"[OPEN] Waiting {OPEN_DELAY_SECONDS}s for content to load...")
    time.sleep(OPEN_DELAY_SECONDS)


def validate_stream_url(url):
    """Check if the URL looks like a valid stream/website URL."""
    return url.startswith("http://") or url.startswith("https://")


def show_menu():
    """Display an interactive menu to choose the audio source."""
    print("\nWhat do you want to fact-check?\n")
    print("  1. YouTube / YouTube Live")
    print("  2. Google Meet call")
    print("  3. Local video file")
    print("  4. Live stream (any URL)")
    print()

    options = {
        "1": ("youtube", "Enter YouTube URL: ", validate_youtube_url, "Invalid YouTube URL. Must contain youtube.com or youtu.be."),
        "2": ("meet", "Enter Google Meet URL: ", validate_meet_url, "Invalid Meet URL. Must contain meet.google.com."),
        "3": ("local", "Enter path to video file: ", validate_local_file, "File not found. Please check the path and try again."),
        "4": ("stream", "Enter stream URL: ", validate_stream_url, "Invalid URL. Must start with http:// or https://."),
    }

    while True:
        choice = input("Enter choice (1/2/3/4): ").strip()
        if choice in options:
            break
        print("Invalid choice. Please enter 1, 2, 3, or 4.\n")

    source_type, prompt, validator, error_msg = options[choice]

    while True:
        target = input(prompt).strip()
        if validator(target):
            break
        print(f"{error_msg}\n")

    open_content(source_type, target)


def init_session():
    """Request the backend to create a capture session."""
    try:
        print(f"[INIT] Connecting to backend at {BACKEND_URL}...")
        resp = requests.post(f"{BACKEND_URL}/init-session", json={}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] Cannot connect to backend at {BACKEND_URL}")
        print("  Make sure the backend is running: python backend.py")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Failed to init session: {e}")
        sys.exit(1)


async def run_capture(token, session_id):
    """Run the CaptureClient to stream system audio for fact-checking."""
    print("\n[CAPTURE] Starting Capture Client...")

    client = None
    stop_event = asyncio.Event()
    cleanup_done = asyncio.Event()

    def handle_signal():
        print("\n[SIGNAL] Received stop signal, initiating shutdown...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    capture_failed = False
    try:
        client = CaptureClient(client_token=token)
        # Request OS permissions
        print("[CAPTURE] Requesting permissions...")
        await client.request_permission("microphone")
        await client.request_permission("screen_capture")

        # Discover available channels
        print("[CAPTURE] Discovering channels...")
        channels = await client.list_channels()

        mic = channels.mics.default
        display = channels.displays.default
        system_audio = channels.system_audio.default

        # Persist captured media to VideoDB after session stops
        for ch in [mic, display, system_audio]:
            if ch:
                ch.store = True

        selected_channels = [c for c in [mic, display, system_audio] if c]
        if not selected_channels:
            print("[ERROR] No capture channels found.")
            capture_failed = True
            return

        print(f"[CAPTURE] Starting with {len(selected_channels)} channel(s):")
        for ch in selected_channels:
            print(f"  - {ch.type}: {ch.id}")

        # Start capture — use start_capture_session if available (videodb >=0.4.3),
        # otherwise fall back to sending the startRecording command directly
        # for compatibility with older SDK versions.
        if hasattr(client, "start_capture_session"):
            await client.start_capture_session(
                capture_session_id=session_id,
                channels=selected_channels,
                primary_video_channel_id=display.id if display else None,
            )
        else:
            payload = {
                "sessionId": session_id,
                "uploadToken": client.client_token,
                "channels": [ch.to_dict() for ch in selected_channels],
            }
            if display:
                payload["primary_video_channel_id"] = display.id
            await client._send_command("startRecording", payload)

        print("[CAPTURE] Recording... Press Ctrl+C to stop.\n")

        # Wait for stop signal
        await stop_event.wait()

    except asyncio.CancelledError:
        print("\n[CAPTURE] Cancelled.")
    except KeyboardInterrupt:
        print("\n[CAPTURE] Stopped by user.")
    except Exception as e:
        print(f"[ERROR] Capture error: {e}")
        import traceback
        traceback.print_exc()
        capture_failed = True
    finally:
        if client:
            print("\n[CLEANUP] Stopping capture...")
            binary_already_exited = False

            try:
                print("  Sending stop signal to server...")
                await asyncio.wait_for(client.stop_capture(), timeout=5.0)
                print("  Stop signal sent.")
                print("  Waiting for server to finalize...")
                await asyncio.sleep(3)
                print("  Capture stopped.")
            except asyncio.TimeoutError:
                print("  Stop timed out (binary may have already exited).")
                binary_already_exited = True
                await asyncio.sleep(3)
            except Exception as e:
                print(f"  Error during stop: {e}")
                await asyncio.sleep(3)
            finally:
                if binary_already_exited:
                    print("  Skipping shutdown (binary already terminated).")
                else:
                    try:
                        print("  Shutting down client...")
                        await asyncio.wait_for(client.shutdown(), timeout=3.0)
                        print("  Client shutdown complete.")
                    except asyncio.TimeoutError:
                        print("  Shutdown timed out.")
                    except Exception as e:
                        print(f"  Shutdown error: {e}")

        cleanup_done.set()
        print("\n[DONE] Cleanup complete.")
        if capture_failed:
            sys.exit(1)


async def main():
    print("=" * 60)
    print("  FACT CHECKER - Capture Client")
    print("=" * 60)

    show_menu()

    session_data = init_session()
    token = session_data["token"]
    session_id = session_data["session_id"]

    print("[INIT] Session created.")
    print(f"  Token: {token[:10]}...")
    print(f"  Session ID: {session_id}\n")

    try:
        await run_capture(token, session_id)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[WARN] Force quit. Session may be left orphaned.")
        sys.exit(1)
