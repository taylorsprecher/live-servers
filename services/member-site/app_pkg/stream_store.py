"""Stream Mic storage — saves microphone chunks streamed from the
phone page (/members/stream) under STREAM_DIR, one folder per
session, with a session log. Built 2026-11-24 at Taylor's direction
("Create a python to allow streaming").

Access model: open access (kept 2026-11-25 at Taylor's direction
when the stream code reverted to this original build) — the routes
serve the page and accept uploads without a sign-in, and this
module validates every field it is handed — session ids are a short
[a-z0-9-] token, sequence numbers are bounded ints, extensions are an
allowlist, and a chunk is capped at 5 MB (a 10-second chunk is far
smaller). Nothing here ever reads outside STREAM_DIR.
"""
import os
import re
import time

from .config import STREAM_DIR

_SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
_EXTS = {"mp4", "webm", "ogg", "wav", "m4a", "bin"}
MAX_CHUNK_BYTES = 5 * 1024 * 1024
MAX_SEQ = 100000


def save_stream_chunk(session, seq, ext, data):
    """Save one chunk; return (ok, message). Never raises."""
    try:
        if not _SESSION_RE.match(session or ""):
            return False, "bad session"
        seq = int(seq)
        if not (0 <= seq <= MAX_SEQ):
            return False, "bad seq"
        ext = (ext or "").lower()
        if ext not in _EXTS:
            return False, "bad ext"
        if not data or len(data) > MAX_CHUNK_BYTES:
            return False, "bad size"
        folder = os.path.join(STREAM_DIR, session)
        os.makedirs(folder, exist_ok=True)
        name = "chunk-%04d.%s" % (seq, ext)
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(data)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(os.path.join(folder, "session.log"), "a", encoding="utf-8") as fh:
            fh.write("%s seq=%d file=%s bytes=%d\n" % (stamp, seq, name, len(data)))
        return True, name
    except (OSError, ValueError) as exc:
        return False, "save failed: %s" % exc
