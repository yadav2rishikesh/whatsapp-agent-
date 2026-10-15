"""Vercel entrypoint: exposes the FastAPI app as a Python function.

vercel.json rewrites /api/* here; the frontend is served as static files.
Database setup runs once per instance in the app's startup hook (app/main.py).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.main import app  # noqa: E402,F401
