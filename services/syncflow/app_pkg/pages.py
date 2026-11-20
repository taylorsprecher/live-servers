"""SyncFlow — page assets: index.html (the single-page client, with
its __BASE_PATH__ placeholder substituted per request, exactly as
the original app served it) and the vendored Fleet design-system
copy at assets/fleet.css."""
from pathlib import Path

_SERVICE_ROOT = Path(__file__).parent.parent

INDEX_HTML = (_SERVICE_ROOT / "index.html").read_text(encoding="utf-8")
FLEET_CSS = (_SERVICE_ROOT / "assets" / "fleet.css").read_bytes()
