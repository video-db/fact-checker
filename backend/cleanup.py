"""Stop active VideoDB streams and free port 5002.

Usage:
    python cleanup.py          # interactive (asks before stopping)
    python cleanup.py --force  # stop everything without prompting
"""

import os
import signal
import subprocess
import sys

import videodb
from config import VIDEO_DB_API_KEY, PORT


def kill_port(port):
    """Kill any process listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
        )
        pids = result.stdout.strip().split()
        if not pids or pids == [""]:
            print(f"[PORT] No process on port {port}.")
            return
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        print(f"[PORT] Killed {len(pids)} process(es) on port {port}.")
    except Exception as e:
        print(f"[PORT] Error: {e}")


def stop_streams(force=False):
    """Find and stop all active VideoDB streams."""
    if not VIDEO_DB_API_KEY:
        print("[ERROR] VIDEO_DB_API_KEY not set. Check your .env file.")
        sys.exit(1)

    conn = videodb.connect(api_key=VIDEO_DB_API_KEY)
    collections = conn.get_collections()

    all_rtstreams = []
    active_rtstreams = []
    status_counts = {}

    for collection in collections:
        try:
            for rts in collection.list_rtstreams():
                all_rtstreams.append(rts)
                status_counts[rts.status] = status_counts.get(rts.status, 0) + 1
                if rts.status == "connected":
                    active_rtstreams.append(rts)
        except Exception:
            pass

    print(f"\n  Total streams:  {len(all_rtstreams)}")
    print(f"  By status:      {status_counts}")
    print(f"  Active streams: {len(active_rtstreams)}")

    if not active_rtstreams:
        print("\n  No active streams to stop.")
        return

    if not force:
        response = (
            input(f"\n  Stop all {len(active_rtstreams)} active stream(s)? (yes/no): ")
            .strip()
            .lower()
        )
        if response not in ("yes", "y"):
            print("  Cancelled.")
            return

    stopped = 0
    failed = 0
    for rts in active_rtstreams:
        try:
            rts.stop()
            stopped += 1
        except Exception:
            failed += 1

    still_active = len(active_rtstreams) - stopped
    print(
        f"\n  Were active: {len(active_rtstreams)}  |  "
        f"Closed: {stopped}  |  Still open: {still_active}"
    )

    if still_active > 0:
        print("  Some streams didn't close. Run the script again to retry.")


def main():
    force = "--force" in sys.argv

    print("=" * 50)
    print("  FACT CHECKER - Cleanup")
    print("=" * 50)

    print(f"\n[1/2] Killing port {PORT}...")
    kill_port(PORT)

    print(f"\n[2/2] Checking VideoDB streams...")
    stop_streams(force=force)

    print("\n  Cleanup done.\n")


if __name__ == "__main__":
    main()
