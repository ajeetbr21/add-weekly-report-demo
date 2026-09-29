#!/usr/bin/env bash
# Runs the Section-49 end-to-end demo against the running compose stack (from the atlas/ directory):
#   docker compose up -d && scripts/demo.sh
# Creates a scratch file in the workspace volume to exercise the approval flow (the demo approves it).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T mcp-local sh -c 'mkdir -p /workspace/notes && echo "scratch file for the approval demo" > /workspace/notes/demo-delete-me.txt'
docker compose exec -T \
  -e ATLAS_URL=http://localhost:8000 \
  -e ATLAS_UI_URL=http://frontend \
  -e DEMO_APPROVAL_FILE=notes/demo-delete-me.txt \
  api python scripts/e2e_demo.py
