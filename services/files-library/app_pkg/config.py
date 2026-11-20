"""Files Library — environment configuration.

Same env names and defaults as the original single-file app.py:
BASE_PATH (default /files), PORT (default 8000), and
FILESLIB_DATA_DIR (default /data).
"""
import os
from pathlib import Path

BASE_PATH = os.environ.get("BASE_PATH", "/files")
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = Path(os.environ.get("FILESLIB_DATA_DIR", "/data"))
