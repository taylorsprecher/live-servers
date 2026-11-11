#!/usr/bin/env bash
# Host wrapper: run ops/stage-course-zip.py inside the live-members
# container, where /data is the members-data volume. Read-mostly: the
# python script copies the course zip into the two product content
# dirs and MD5-verifies each copy (see its docstring).
set -eu
docker exec -i live-members python3 - < /home/deploy/stage-course-zip.py
echo "--- content dirs ---"
docker exec live-members ls -l /data/content/ccce /data/content/emspr
