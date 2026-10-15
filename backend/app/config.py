"""Runtime configuration, all from environment variables."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Blank -> SQLite. On Vercel the code folder is read-only, so SQLite goes to /tmp
# (temporary: resets on cold start). Set DATABASE_URL (Postgres) to keep data.
_sqlite_dir = "/tmp" if os.getenv("VERCEL") else str(BASE_DIR)
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{_sqlite_dir}/portfolio.db"
# Pin the Postgres driver to psycopg2 (installed via requirements). SQLAlchemy 2.1
# defaults bare postgresql:// URLs to psycopg v3, which isn't installed. Also
# accept the postgres:// scheme some hosts hand out.
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len(_prefix):]
        break

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# OpenRouter provider routing: "latency" = lowest-latency provider first
# (docs: openrouter.ai/docs/features/provider-routing). Empty = OpenRouter default.
OPENROUTER_PROVIDER_SORT = os.getenv("OPENROUTER_PROVIDER_SORT", "latency")

# Model routing. Fast model for simple lookups/CRUD, strong model for
# comparisons, what-ifs and multi-step reasoning, fallback if a call fails.
MODEL_FAST = os.getenv("MODEL_FAST", "openai/gpt-4.1-mini")
MODEL_STRONG = os.getenv("MODEL_STRONG", "openai/gpt-4.1")
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "google/gemini-2.5-flash")

LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "30"))
# Cap reply length. Without it OpenRouter reserves credit for the model's full
# output window (64k tokens) on every call and rejects low-balance keys with 402.
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1200"))
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "6"))
# How many previous turns are replayed in full (with tool calls/results).
FULL_HISTORY_TURNS = int(os.getenv("FULL_HISTORY_TURNS", "2"))
# Older turns are replayed as user/assistant text only, up to this many.
TEXT_HISTORY_TURNS = int(os.getenv("TEXT_HISTORY_TURNS", "6"))

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # optional; empty = admin API open
DATA_DIR = BASE_DIR / "data"
