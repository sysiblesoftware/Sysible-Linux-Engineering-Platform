#!/usr/bin/env bash
# Run both SLEP services in one container: the backend on loopback, the console
# on the exposed port (it proxies to the backend). If either process exits, the
# container exits so Docker's restart policy brings it back cleanly.
set -euo pipefail

: "${SLEP_BACKEND_PORT:=9100}"
: "${SLEP_CONSOLE_PORT:=8810}"

uvicorn backend.app:app --host 127.0.0.1 --port "${SLEP_BACKEND_PORT}" &
backend_pid=$!
uvicorn webgui.server:app --host 0.0.0.0 --port "${SLEP_CONSOLE_PORT}" &
bff_pid=$!

term() { kill "$backend_pid" "$bff_pid" 2>/dev/null || true; }
trap term TERM INT

# Exit as soon as either service dies (wait -n), then clean up the other.
wait -n
term
