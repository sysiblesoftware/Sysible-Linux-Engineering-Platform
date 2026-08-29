"""SLEP web console — BFF (:8810).

Serves the React SPA and proxies /api/* to the backend (:9100). Mirrors the
Sysible Controller console split: the browser talks only to this service, which
forwards to the backend. Auth for the MVP is a bearer token the SPA obtains from
the backend's /login and sends back through this proxy.

Serving order: the built Vite SPA at frontend/dist if present; otherwise the
no-build static console at static/index.html (so SLEP runs with zero node
toolchain — the "less of a hassle" promise extends to the console too).
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BACKEND = os.environ.get("SLEP_BACKEND_URL", "http://127.0.0.1:9100").rstrip("/")
HERE = Path(__file__).resolve().parent
DIST = HERE / "frontend" / "dist"
STATIC = HERE / "static"

app = FastAPI(title="SLEP Console")

_HOP = {"content-length", "transfer-encoding", "connection", "host"}
_MAX_REQUEST_BYTES = int(os.environ.get("SLEP_MAX_REQUEST_BYTES", str(16 * 1024 * 1024)))

# SLOP SSO trust boundary (see backend/app.py). The backend trusts X-Sysible-User /
# X-Sysible-Role only when they arrive with a valid X-Sysible-Auth shared secret. A
# direct browser hitting this console must NEVER be able to forge those identity
# headers, so the BFF strips ALL inbound X-Sysible-* before forwarding. They are re-
# added only when trust mode is on AND the request already carries the correct shared
# secret — i.e. it genuinely came in on Caddy's hop into the console (Caddy sets these
# headers itself). The BFF can't tell "from Caddy" from "from browser" except by that
# secret, so the secret IS the gate. Trust mode off → all three are always dropped.
_TRUST_GATEWAY_AUTH = os.environ.get("SLEP_TRUST_GATEWAY_AUTH", "0") == "1"
_SSO_SHARED_SECRET = os.environ.get("SYSIBLE_SSO_SHARED_SECRET", "")
_SSO_HEADERS = ("x-sysible-user", "x-sysible-role", "x-sysible-auth")

# CSP for the SPA: self-hosted only (airgap-friendly). Monaco runs its language
# services in blob workers and uses eval in its tokenizer, so worker-src blob:
# and script-src 'unsafe-eval' are required; images/fonts allow data: URIs.
_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-eval'; worker-src 'self' blob:; font-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")


@app.middleware("http")
async def guard(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_REQUEST_BYTES:
        return JSONResponse({"detail": "Request body too large."}, status_code=413)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    if request.url.scheme == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return resp


@app.get("/api/health")
def health():
    try:
        r = requests.get(f"{BACKEND}/healthz", timeout=5)
        return JSONResponse(r.json(), status_code=r.status_code)
    except requests.exceptions.RequestException as e:
        return JSONResponse({"status": "backend-unreachable", "error": str(e)}, status_code=502)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    """Forward /api/* to the backend, preserving method, query, body, and the
    Authorization bearer. Strips the /api prefix (the backend has no /api).

    Async so we can await the request body; the blocking `requests` call is run in
    a threadpool so it doesn't stall the event loop. Fine for a single-operator
    console; swap for httpx.AsyncClient if concurrency ever matters."""
    import anyio

    url = f"{BACKEND}/{path}"
    # Drop any client-supplied X-Sysible-* so a browser can't spoof a gateway identity;
    # re-add them below only for a request that already proves it came through the gateway.
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP and k.lower() not in _SSO_HEADERS}
    if _TRUST_GATEWAY_AUTH and _SSO_SHARED_SECRET and \
            secrets.compare_digest(request.headers.get("x-sysible-auth", ""), _SSO_SHARED_SECRET):
        # Legitimate gateway hop (valid shared secret) — forward the identity headers.
        for h in _SSO_HEADERS:
            v = request.headers.get(h)
            if v is not None:
                headers[h] = v
    body = await request.body()
    params = dict(request.query_params)

    def _do():
        return requests.request(request.method, url, params=params, data=body,
                                headers=headers, timeout=120)
    try:
        raw = await anyio.to_thread.run_sync(_do)
    except requests.exceptions.RequestException as e:
        return JSONResponse({"detail": f"backend unreachable: {e}"}, status_code=502)
    out_headers = {k: v for k, v in raw.headers.items() if k.lower() not in _HOP}
    return Response(content=raw.content, status_code=raw.status_code, headers=out_headers)


# ---- static SPA (mounted last so /api wins) --------------------------------
def _index() -> Path:
    if (DIST / "index.html").exists():
        return DIST / "index.html"
    return STATIC / "index.html"


@app.get("/")
def index():
    return FileResponse(_index())


# Serve built assets if present.
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/{path:path}")
def spa(path: str):
    # SPA fallback: any non-API path serves index.html (client-side routing).
    # SECURITY: resolve the requested path and confine it to the served root before
    # serving a file. Without this, a percent-encoded traversal (e.g. /%2e%2e/%2e%2e/
    # data/vault.key) survives URL normalisation, decodes to '..' here, and would let
    # an UNAUTHENTICATED caller read arbitrary files — including the vault key and the
    # SQLite DB. Anything that escapes the root falls through to index.html.
    base = (DIST if DIST.exists() else STATIC).resolve()
    try:
        target = (base / path.lstrip("/")).resolve()
    except (OSError, ValueError):
        return FileResponse(_index())
    if (target == base or base in target.parents) and target.is_file():
        return FileResponse(target)
    return FileResponse(_index())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SLEP_CONSOLE_PORT", "8810")))
