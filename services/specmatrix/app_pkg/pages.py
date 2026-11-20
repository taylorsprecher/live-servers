"""Spec Matrix — page assets. index.html stays the one self-contained
page (dataset, CSS and JS embedded, exactly as before); the only
change is the Fleet design-system shell in its markup and the
__BASE_PATH__ placeholder in the fleet.css link, substituted here
with the configured base path. The vendored design-system copy is
read from assets/fleet.css beside app.py."""
from pathlib import Path

from .config import BASE_PATH

_SERVICE_ROOT = Path(__file__).parent.parent

INDEX_HTML = (_SERVICE_ROOT / "index.html").read_text(encoding="utf-8") \
    .replace("__BASE_PATH__", BASE_PATH).encode()
FLEET_CSS = (_SERVICE_ROOT / "assets" / "fleet.css").read_bytes()
