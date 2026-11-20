"""Files Library — data layer.

Reads library.json from the service directory, unmodified, in its
existing format ({"entries": [...]}). library.json is SANITIZED at
generation time: held entries carry only file/type/date/group (no
description, no size, no download id), so held content cannot leak
through this service. Download ids exist only for non-held files, so
a held file is unreachable by construction.
"""
import json
import re
from pathlib import Path

_HERE = Path(__file__).parent.parent
_LIBRARY = json.loads((_HERE / "library.json").read_text(encoding="utf-8"))
ENTRIES = _LIBRARY["entries"]

DL_ID = re.compile(r"^f\d{4}$")
# download id -> (original filename, size)
FILES = {}
for _e in ENTRIES:
    if _e.get("dl"):
        FILES[_e["dl"]] = (_e["file"], _e.get("size"))

HEALTH = {
    "status": "ok",
    "service": "files-library",
    "entries": len(ENTRIES),
    "downloadable_files": len(FILES),
    "held": sum(1 for e in ENTRIES if e.get("held")),
}
