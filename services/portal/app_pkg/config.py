"""Portal — environment configuration.

Same env names and defaults as the original single-file app.py:
BASE_PATH (default /portal) and PORT (default 8000).
"""
import os

BASE_PATH = os.environ.get("BASE_PATH", "/portal")
PORT = int(os.environ.get("PORT", "8000"))
