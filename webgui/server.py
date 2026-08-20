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


@app.get("/api/health")
def health():
    try:
        r = requests.get(f"{BACKEND}/healthz", timeout=5)
        return JSONResponse(r.json(), status_code=r.status_code)
    except requests.exceptions.RequestException as e:
        return JSONResponse({"status": "backend-unreachable", "error": str(e)}, status_code=502)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    """Forward /api/* to the backend, preserving method, query, body, and the
    Authorization bearer. Strips the /api prefix (the backend has no /api).

    Async so we can await the request body; the blocking `requests` call is run in
    a threadpool so it doesn't stall the event loop. Fine for a single-operator
    console; swap for httpx.AsyncClient if concurrency ever matters."""
    import anyio

    url = f"{BACKEND}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
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
    target = (DIST / path) if DIST.exists() else (STATIC / path)
    if target.is_file():
        return FileResponse(target)
    return FileResponse(_index())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SLEP_CONSOLE_PORT", "8810")))
