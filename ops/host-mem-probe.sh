#!/usr/bin/env bash
# Read-only host memory probe. Changes nothing: reports free memory,
# per-container usage (all projects), and the fattest host processes.
set -u
echo "=== free -m ==="
free -m
echo "=== docker stats (all containers, one sample) ==="
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}'
echo "=== top 12 processes by RSS ==="
ps -eo pid,comm,rss --sort=-rss | head -13
echo "=== docker ps ==="
docker ps --format 'table {{.Names}}\t{{.Status}}'
