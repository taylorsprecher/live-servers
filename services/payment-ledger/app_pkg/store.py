"""Data layer: the single shared psycopg connection, the global
write lock, schema application, and small shared helpers.

Moved verbatim from the monolith; the connection and lock remain
module-level singletons so every caller shares exactly one
connection and one lock, as before."""

import threading
import uuid

import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL, SCHEMA_PATH

_lock = threading.Lock()
_conn = None


def db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False)
    return _conn


def split_sql(text):
    """Split a SQL script on top-level semicolons, respecting
    $$ ... $$ function bodies and -- line comments."""
    stmts, buf, i, in_dollar = [], [], 0, False
    lines = text.split("\n")
    for line in lines:
        stripped = line.strip()
        if not in_dollar and stripped.startswith("--"):
            continue
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and stripped.endswith(";"):
            stmts.append("\n".join(buf))
            buf = []
    if buf:
        stmts.append("\n".join(buf))
    return [s for s in (x.strip() for x in stmts) if s]


def apply_schema():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        script = fh.read()
    conn = db()
    with conn.cursor() as cur:
        for stmt in split_sql(script):
            cur.execute(stmt)
    conn.commit()


def new_uuid():
    return str(uuid.uuid4())
