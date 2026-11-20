#!/usr/bin/env python3
"""Zillion Storage — entry shim.

The implementation that used to live in this single file now lives in
the backend package modules beside it (config.py, common.py,
aws_backend.py, minio_backend.py, s3compat.py, backends.py, web.py),
moved function-for-function with no logic changes. This shim keeps
the container contract unchanged: CMD ["python3", "backend/server.py"].
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web import main  # noqa: E402

if __name__ == "__main__":
    main()
