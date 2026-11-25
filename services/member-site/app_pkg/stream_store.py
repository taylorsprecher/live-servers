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


def list_stream_sessions():
    """Read side (Jasmine's access, 2026-11-25): every session folder
    under STREAM_DIR with its chunk count, highest sequence, byte
    total, and last-write time. Names are re-validated against the
    session regex; anything else in the directory is ignored."""
    out = []
    try:
        names = os.listdir(STREAM_DIR)
    except OSError:
        return out
    for name in names:
        if not _SESSION_RE.match(name):
            continue
        folder = os.path.join(STREAM_DIR, name)
        if not os.path.isdir(folder):
            continue
        chunks = 0
        last_seq = -1
        total = 0
        updated = 0.0
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            m = re.match(r"^chunk-(\d{4,6})\.([a-z0-9]+)$", entry)
            if not m or m.group(2) not in _EXTS:
                continue
            try:
                st = os.stat(os.path.join(folder, entry))
            except OSError:
                continue
            chunks += 1
            last_seq = max(last_seq, int(m.group(1)))
            total += st.st_size
            updated = max(updated, st.st_mtime)
        out.append({
            "session": name,
            "chunks": chunks,
            "last_seq": last_seq,
            "bytes": total,
            "updated": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated)) if updated else None,
        })
    out.sort(key=lambda s: s["updated"] or "", reverse=True)
    return out


def stream_chunk_path(session, seq):
    """Resolve one saved chunk to (path, ext) or (None, None). The
    session is re-validated and the sequence bounded exactly as on
    the write side, and the extension comes from the file actually
    on disk — a caller can never steer this outside STREAM_DIR."""
    if not _SESSION_RE.match(session or ""):
        return None, None
    try:
        seq = int(seq)
    except (TypeError, ValueError):
        return None, None
    if not (0 <= seq <= MAX_SEQ):
        return None, None
    folder = os.path.join(STREAM_DIR, session)
    for ext in sorted(_EXTS):
        path = os.path.join(folder, "chunk-%04d.%s" % (seq, ext))
        if os.path.isfile(path):
            return path, ext
    return None, None


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
