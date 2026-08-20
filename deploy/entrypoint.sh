#!/usr/bin/env bash
# Run both SLEP services in one container: the backend on loopback, the console
# on the exposed port (it proxies to the backend). If either process exits, the
# container exits so Docker's restart policy brings it back cleanly.
#
# The console is served over HTTPS by default with a self-signed cert (SLEP_TLS=1)
# — HTTPS makes the browser treat the page as a secure context, which the Monaco
# IDE needs for clipboard/paste, and it encrypts the console across the network.
# Set SLEP_TLS=0 to serve plain HTTP (e.g. behind a TLS-terminating proxy).
set -euo pipefail

: "${SLEP_BACKEND_PORT:=9100}"
: "${SLEP_CONSOLE_PORT:=8810}"
: "${SLEP_DATA_DIR:=/data}"
: "${SLEP_TLS:=1}"
: "${SLEP_TLS_HOSTS:=}"

uvicorn backend.app:app --host 127.0.0.1 --port "${SLEP_BACKEND_PORT}" &
backend_pid=$!

bff_ssl=()
if [[ "${SLEP_TLS}" != "0" ]]; then
  # Self-signed cert scoped to localhost/hostname + any SLEP_TLS_HOSTS, generated
  # once under the data volume (delete data/tls to regenerate). Prints cert\nkey.
  readarray -t _cert < <(python3 -m backend.selfsigned "${SLEP_DATA_DIR}/tls" ${SLEP_TLS_HOSTS//,/ })
  bff_ssl=(--ssl-certfile "${_cert[0]}" --ssl-keyfile "${_cert[1]}")
  echo "[entrypoint] console: https://0.0.0.0:${SLEP_CONSOLE_PORT} (self-signed TLS)"
else
  echo "[entrypoint] console: http://0.0.0.0:${SLEP_CONSOLE_PORT} (TLS disabled)"
fi
uvicorn webgui.server:app --host 0.0.0.0 --port "${SLEP_CONSOLE_PORT}" "${bff_ssl[@]}" &
bff_pid=$!

term() { kill "$backend_pid" "$bff_pid" 2>/dev/null || true; }
trap term TERM INT

# Exit as soon as either service dies (wait -n), then clean up the other.
wait -n
term
