#!/usr/bin/env python3
"""SyncFlow — Backend–Frontend Sync Flow demo (Taylor's spec build).

Entry shim: the implementation lives in the app_pkg package
(config / store / pages / routes / server), split from the original
single-file app with no behavior change. The container contract is
unchanged: CMD ["python3", "app.py"].

Implements the "Backend–Frontend Sync Flow" diagram Taylor posted on
2026-11-12: Frontend Client -> API Gateway -> Sync & Conflict
Handling, with Client Updates over the Event Bus / Event Stream.
One demo profile record is stored with an integer version (starts
at 1). State persists to a JSON file in SYNCFLOW_DATA_DIR so it
survives container restarts. Events are only ever appended when the
corresponding thing actually happens; a rollback is never faked.
Demo data only — the profile is invented and neutral.

Python 3 stdlib ONLY (http.server).
"""
from app_pkg.server import main

if __name__ == "__main__":
    main()
