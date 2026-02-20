import os

from dotenv import load_dotenv

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
load_dotenv(os.path.join(_PROJECT_ROOT, '.env'))

# Required
VIDEO_DB_API_KEY = os.getenv("VIDEO_DB_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Server
try:
    PORT = int(os.getenv("PORT", "5002"))
except ValueError:
    print(f"[ERROR] Invalid PORT value: {os.getenv('PORT')!r} — must be a number. Falling back to 5002.")
    PORT = 5002

# Fact-check timing
FACT_CHECK_INTERVAL = int(os.getenv("FACT_CHECK_INTERVAL", "20"))
MIN_WORDS_FOR_CHECK = int(os.getenv("MIN_WORDS_FOR_CHECK", "15"))

# Logging
LOG_DIR = "logs"

# Pipeline settings
GEMINI_MODEL = "gemini-2.0-flash"
CONFIDENCE_THRESHOLD = os.getenv("CONFIDENCE_THRESHOLD", "high")
CONTEXT_WINDOW_WORDS = int(os.getenv("CONTEXT_WINDOW_WORDS", "150"))
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "30"))
