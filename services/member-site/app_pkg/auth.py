"""Passwords and sessions — PBKDF2 hashing and token code moved
verbatim from the monolith, never reimplemented."""
import hashlib
import secrets
import time

from .config import PBKDF2_ROUNDS, SESSION_DAYS
from .store import _db, _lock


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), dk.hex()


def verify_password(password, salt_hex, hash_hex):
    _, candidate = hash_password(password, bytes.fromhex(salt_hex))
    return secrets.compare_digest(candidate, hash_hex)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(member_id):
    token = secrets.token_urlsafe(32)
    with _lock, _db:
        _db.execute(
            "INSERT INTO sessions (token_hash, member_id, expires_at) VALUES (?, ?, ?)",
            (token_hash(token), member_id, int(time.time()) + SESSION_DAYS * 86400),
        )
    return token


def member_for_token(token):
    if not token:
        return None
    with _lock:
        row = _db.execute(
            "SELECT m.* FROM sessions s JOIN members m ON m.id = s.member_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash(token), int(time.time())),
        ).fetchone()
    return row
