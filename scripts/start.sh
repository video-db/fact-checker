#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$APP_DIR/backend"

echo "============================================================"
echo "  Fact Checker"
echo "============================================================"
echo ""

# Pre-flight checks
MISSING=0

if [ ! -d "$APP_DIR/node_modules" ]; then
  echo "[ERROR] Node.js dependencies not installed."
  MISSING=1
fi

if [ ! -d "$BACKEND_DIR/venv" ]; then
  echo "[ERROR] Python virtual environment not found."
  MISSING=1
fi

if [ ! -f "$APP_DIR/.env" ]; then
  echo "[ERROR] .env file not found."
  MISSING=1
fi

if [ "$MISSING" -eq 1 ]; then
  echo ""
  echo "  Run setup first:  ./scripts/setup.sh"
  exit 1
fi

# Kill stale processes on port 5002
if lsof -ti:5002 &>/dev/null; then
  echo "[CLEANUP] Killing stale process on port 5002..."
  lsof -ti:5002 | xargs kill -15 2>/dev/null || true
  sleep 2
  # Force kill if still alive
  if lsof -ti:5002 &>/dev/null; then
    lsof -ti:5002 | xargs kill -9 2>/dev/null || true
  fi
fi

# Launch
echo "[START] Launching Fact Checker..."
cd "$APP_DIR"
npx electron .
