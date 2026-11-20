#!/usr/bin/env python3
"""Files Library — Taylor's file index with downloads (static + streamer).

Thin shim (Fleet redesign v2): the implementation lives in app_pkg/
(config.py — env, store.py — library.json data layer, pages.py —
listing rendering, routes.py — dispatch incl. streamed downloads,
server.py — bootstrap). Behavior is unchanged from the original
single-file app: Python 3 stdlib ONLY (http.server); serves one
listing page under the /files base path — every entry from
library.json, grouped by project with a client-side search filter —
plus one download route per file present on disk. Downloads are
streamed from FILESLIB_DATA_DIR in 256 KB chunks (never loaded whole
into memory). Stateless: nothing is stored; the listing is rendered
once at startup.
"""
from app_pkg.server import main

if __name__ == "__main__":
    main()
