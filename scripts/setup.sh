#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$APP_DIR/backend"

echo "============================================================"
echo "  Fact Checker - Setup"
echo "============================================================"
echo ""

# ---------------------------------------------------------------
# 1. Check / install system dependencies
# ---------------------------------------------------------------

# Python 3.12+
if command -v python3 &>/dev/null; then
  PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
  PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
  if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 12 ]; then
    echo "[OK] Python $PY_VERSION found."
  else
    echo "[WARN] Python $PY_VERSION found, but 3.12+ is recommended."
  fi
else
  echo "[MISSING] Python 3 not found."
  if [[ "$OSTYPE" == "darwin"* ]] && command -v brew &>/dev/null; then
    echo "[SETUP] Installing Python 3.12 via Homebrew..."
    brew install python@3.12
  else
    echo "  Please install Python 3.12+: https://www.python.org/downloads/"
    exit 1
  fi
fi

# Node.js 18+
if command -v node &>/dev/null; then
  NODE_VERSION=$(node -v | sed 's/v//' | cut -d. -f1)
  echo "[OK] Node.js v$(node -v | sed 's/v//') found."
  if [ "$NODE_VERSION" -lt 18 ]; then
    echo "[WARN] Node.js 18+ is required. Please upgrade: https://nodejs.org"
    exit 1
  fi
else
  echo "[MISSING] Node.js not found."
  if [[ "$OSTYPE" == "darwin"* ]] && command -v brew &>/dev/null; then
    echo "[SETUP] Installing Node.js via Homebrew..."
    brew install node
  else
    echo "  Please install Node.js 18+: https://nodejs.org"
    exit 1
  fi
fi

# uv (Python package manager)
if ! command -v uv &>/dev/null; then
  echo "[SETUP] Installing uv (fast Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
else
  echo "[OK] uv found."
fi

echo ""

# ---------------------------------------------------------------
# 2. API keys
# ---------------------------------------------------------------

ENV_FILE="$APP_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  echo "[OK] .env file found. To re-enter keys, delete it and run setup again."
  echo ""
else
  echo "You'll need two free API keys:"
  echo "  - VideoDB:  https://console.videodb.io"
  echo "  - Gemini:   https://aistudio.google.com/apikey"
  echo ""

  read -rp "  VideoDB API Key: " VIDEODB_KEY
  read -rp "  Gemini API Key:  " GEMINI_KEY
  echo ""

  if [ -n "$VIDEODB_KEY" ] && [ -n "$GEMINI_KEY" ]; then
    cat > "$ENV_FILE" <<EOF
VIDEO_DB_API_KEY=$VIDEODB_KEY
GEMINI_API_KEY=$GEMINI_KEY
EOF
    echo "[OK] API keys saved to .env"
  else
    echo "[WARN] Skipped — create .env manually before running."
    echo "  Copy the template:  cp .env.example .env"
  fi
fi

# ---------------------------------------------------------------
# 3. Install Node.js dependencies
# ---------------------------------------------------------------

echo "[SETUP] Installing Node.js dependencies..."
cd "$APP_DIR"
npm install --no-audit --no-fund 2>&1 | tail -1

# ---------------------------------------------------------------
# 4. Python virtual environment + dependencies
# ---------------------------------------------------------------

if [ ! -d "$BACKEND_DIR/venv" ]; then
  echo "[SETUP] Creating Python virtual environment..."
  uv venv "$BACKEND_DIR/venv" --python 3.12
fi

echo "[SETUP] Installing Python dependencies..."
uv pip install -r "$BACKEND_DIR/requirements.txt" \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  --python "$BACKEND_DIR/venv/bin/python" --quiet

echo ""
echo "============================================================"
echo "  Setup complete!"
echo ""
echo "  Start the app:  ./scripts/start.sh"
echo "               or: npm start"
echo "============================================================"
