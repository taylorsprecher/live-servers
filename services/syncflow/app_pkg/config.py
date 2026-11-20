"""SyncFlow — configuration. Same env names and defaults as the
original single-file app.py: BASE_PATH (default /syncflow), PORT
(default 8000) and SYNCFLOW_DATA_DIR (default /data)."""
import os
from pathlib import Path

BASE_PATH = os.environ.get("BASE_PATH", "/syncflow")
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = Path(os.environ.get("SYNCFLOW_DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"
