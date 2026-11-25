"""Stream Mic storage — saves microphone chunks streamed from the
phone page (/members/stream) under STREAM_DIR, one folder per
session, with a session log. Built 2026-11-24 at Taylor's direction
("Create a python to allow streaming").

Access model changed 2026-11-25 at Taylor's explicit direction
("Deploy it live, open access"): the page, the upload endpoint, the
session listing and chunk downloads are public — no sign-in. This
module still validates every field it is handed — session ids are a
short [a-z0-9-] token, sequence numbers are bounded ints, extensions
are an allowlist, and a chunk is capped at 5 MB (a 10-second chunk is
far smaller). Nothing here ever reads outside STREAM_DIR.
"""
import json
import os
import re
import time

from .config import STREAM_DIR

_SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
_EXTS = {"mp4", "webm", "ogg", "wav", "m4a", "bin"}
MAX_CHUNK_BYTES = 5 * 1024 * 1024
MAX_SEQ = 100000


def save_stream_chunk(session, seq, ext, data, mode=""):
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
        meta_path = os.path.join(folder, "meta.json")
        if not os.path.exists(meta_path):
            try:
                with open(meta_path, "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "session_id": session,
                            "mode": mode or "unknown",
                            "created_at": stamp,
                            "access": "all",
                        },
                        fh,
                        indent=2,
                    )
            except OSError:
                pass
        return True, name
    except (OSError, ValueError) as exc:
        return False, "save failed: %s" % exc


def _chunk_files(folder):
    try:
        return sorted(
            f
            for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
            and f not in ("session.log", "meta.json")
        )
    except OSError:
        return []


def list_stream_sessions():
    """Public session listing (open access, Taylor's direction
    2026-11-25): every session folder under STREAM_DIR with its chunk
    file names. Never raises."""
    sessions = []
    try:
        names = os.listdir(STREAM_DIR)
    except OSError:
        return sessions
    entries = []
    for name in names:
        folder = os.path.join(STREAM_DIR, name)
        if not os.path.isdir(folder) or not _SESSION_RE.match(name):
            continue
        meta = {}
        try:
            with open(os.path.join(folder, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}
        chunks = _chunk_files(folder)
        try:
            mtime = os.path.getmtime(folder)
        except OSError:
            mtime = 0
        entries.append((mtime, {
            "session_id": name,
            "mode": meta.get("mode", "unknown"),
            "created_at": meta.get("created_at"),
            "chunk_count": len(chunks),
            "chunks": chunks,
        }))
    entries.sort(key=lambda e: e[0], reverse=True)
    return [e[1] for e in entries]


def stream_chunk_path(session, filename):
    """Resolve one chunk file for public download, or None. The
    filename must be a plain basename of a real chunk file inside the
    session's own folder — nothing outside STREAM_DIR is reachable."""
    if not _SESSION_RE.match(session or ""):
        return None
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    if filename in ("session.log", "meta.json"):
        return None
    folder = os.path.join(STREAM_DIR, session)
    fpath = os.path.join(folder, filename)
    if os.path.dirname(os.path.abspath(fpath)) != os.path.abspath(folder):
        return None
    return fpath if os.path.isfile(fpath) else None


def count_stream_chunks(session):
    if not _SESSION_RE.match(session or ""):
        return 0
    return len(_chunk_files(os.path.join(STREAM_DIR, session)))
