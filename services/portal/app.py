#!/usr/bin/env python3
"""Network Portal — 911transedu.net live-status dashboard (static).

Thin shim (Fleet redesign v2): the implementation lives in app_pkg/
(config.py — env, pages.py — COMPANIES map + templates, routes.py —
dispatch, server.py — bootstrap). Behavior is unchanged from the
original single-file app: Python 3 stdlib ONLY (http.server); serves
the portal index page under the /portal base path plus one company
route page per company in COMPANIES, rendered from company.html by
placeholder substitution. On load the pages probe the live-server
subdomains client-side (fetch, no-cors) and show a green/red
reachability dot per card, with a manual Refresh Status button.
Stateless: nothing is stored; the probes run in the visitor's browser.
"""
from app_pkg.server import main

if __name__ == "__main__":
    main()
