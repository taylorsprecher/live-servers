#!/usr/bin/env python3
"""Spec Matrix — cross-brand device & camera spec analyzer (static).

Entry shim: the implementation lives in the app_pkg package
(config / pages / routes / server), split function-for-function from
the original single-file app with no behavior change. The container
contract is unchanged: CMD ["python3", "app.py"].

The page is a faithful port of the React component Taylor pasted on
2026-11-11 (~/workspace/user/files/SpecMatrix_App_Pasted.jsx):
15-device dataset, Side-by-Side matrix, Catalog, Score Radar
analytics and Deep Scanner views, search/brand/category/price
filters, pin-to-compare (max 4), diff highlighting, device modal,
and copy-comparison-report. Stateless: nothing is stored; compare
pins live in the page only. Python 3 stdlib ONLY (http.server).
"""
from app_pkg.server import main

if __name__ == "__main__":
    main()
