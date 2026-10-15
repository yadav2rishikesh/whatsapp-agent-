"""Vercel entrypoint: exposes the FastAPI app as a Python function.

vercel.json rewrites /api/* here; the frontend is served as static files.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.main import app  # noqa: E402
from app.seed import seed  # noqa: E402

try:
    seed()  # create tables + load the CSVs on cold start (no-op if already seeded)
except Exception:  # never take the whole API down: /api/health still answers and logs show why
    logging.getLogger("startup").exception("Database setup failed")
