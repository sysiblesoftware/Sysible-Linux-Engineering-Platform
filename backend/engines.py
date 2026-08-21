"""One-click engine installation.

The container image bakes Ansible/Terraform/Salt in, but when SLEP runs directly
on a host an engine may be missing. Rather than make the operator drop to a shell,
these helpers install an engine into a SLEP-managed location under the data dir and
put it on PATH so the runners (which inherit os.environ) pick it up immediately —
no container rebuild, no root, no touching system paths:

  * Terraform → a static binary in DATA_DIR/engine-bin/
  * Ansible / Salt → a dedicated venv at DATA_DIR/engine-venv/ (pip install)

Installs run on a background thread and stream to DATA_DIR/engine-installs/<e>.log,
which the console tails exactly like a run log.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import db

TERRAFORM_VERSION = "1.9.8"

ENGINES = {
    "ansible": {"label": "Ansible", "binary": "ansible-playbook"},
    "terraform": {"label": "Terraform", "binary": "terraform"},
    "salt": {"label": "Salt", "binary": "salt-ssh"},
}

# The Galaxy collections the built-in task snippets reach for (community.general,
# ansible.posix, community.crypto, community.docker, containers.podman). The full
# `ansible` package bundles these, but a lean install (ansible-core only) or a
# custom environment can miss them — one click via `ansible-galaxy` fills the gap.
COMMON_COLLECTIONS = [
    "community.general",
    "ansible.posix",
    "community.crypto",
    "community.docker",
    "containers.podman",
]
_COLL_KEY = "collections"

_state: dict[str, dict] = {}          # engine -> {"status": running|done|failed}
_lock = threading.Lock()


def _bin_dir() -> Path:
    return db.DATA_DIR / "engine-bin"


def _venv_dir() -> Path:
    return db.DATA_DIR / "engine-venv"


def _log_path(engine: str) -> Path:
    d = db.DATA_DIR / "engine-installs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{engine}.log"


def ensure_path() -> None:
    """Prepend the SLEP-managed engine dirs to PATH so installed engines are found
    by shutil.which and by the runner subprocesses. Idempotent."""
    parts = [str(_venv_dir() / "bin"), str(_bin_dir())]
    cur = os.environ.get("PATH", "")
    have = cur.split(os.pathsep) if cur else []
    add = [p for p in parts if p not in have]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + ([cur] if cur else []))


def is_installed(engine: str) -> bool:
    return shutil.which(ENGINES[engine]["binary"]) is not None


def status() -> dict:
    ensure_path()
    with _lock:
        out = {}
        for e, spec in ENGINES.items():
            st = _state.get(e, {})
            out[e] = {
                "label": spec["label"],
                "binary": spec["binary"],
                "installed": is_installed(e),
                "installing": st.get("status") == "running",
                "last_status": st.get("status"),
            }
        return out


def install_log(engine: str, offset: int = 0):
    p = _log_path(engine)
    if not p.exists():
        return "", 0
    data = p.read_bytes()
    return data[offset:].decode("utf-8", "replace"), len(data)


def start_install(engine: str) -> None:
    if engine not in ENGINES:
        raise ValueError("unknown engine")
    with _lock:
        if _state.get(engine, {}).get("status") == "running":
            return
        _state[engine] = {"status": "running", "started": time.time()}
    threading.Thread(target=_run_install, args=(engine,), daemon=True).start()


# ------------------------------------------------------------------ collections
def _valid_collection(name: str) -> bool:
    """namespace.name — letters, digits, underscores only. Guards the shell-free
    ansible-galaxy call against odd input."""
    parts = str(name).split(".")
    return len(parts) == 2 and all(re.fullmatch(r"[a-z0-9_]+", p or "") for p in parts)


def collections_status() -> dict:
    """What Galaxy collections are installed, plus the curated 'common' set. Reads
    `ansible-galaxy collection list` (JSON); if Ansible isn't installed yet, reports
    that instead of failing."""
    ensure_path()
    galaxy = shutil.which("ansible-galaxy")
    installed: list[str] = []
    if galaxy:
        try:
            out = subprocess.run([galaxy, "collection", "list", "--format", "json"],
                                 capture_output=True, text=True, timeout=60)
            if out.returncode == 0 and out.stdout.strip():
                import json as _json
                data = _json.loads(out.stdout)
                names = set()
                for path_entry in (data or {}).values():
                    if isinstance(path_entry, dict):
                        names.update(path_entry.keys())
                installed = sorted(names)
        except Exception:  # noqa: BLE001 — best-effort listing
            installed = []
    st = _state.get(_COLL_KEY, {})
    return {
        "ansible_installed": bool(galaxy),
        "installed": installed,
        "common": COMMON_COLLECTIONS,
        "missing_common": [c for c in COMMON_COLLECTIONS if c not in installed],
        "installing": st.get("status") == "running",
        "last_status": st.get("status"),
    }


def collections_install_log(offset: int = 0):
    return install_log(_COLL_KEY, offset)


def start_collections_install(names: list[str] | None = None) -> None:
    """Install Galaxy collections via ansible-galaxy on a background thread,
    streaming to the collections install log. Defaults to the curated common set."""
    wanted = [n for n in (names or COMMON_COLLECTIONS) if _valid_collection(n)]
    if not wanted:
        raise ValueError("no valid collections requested")
    with _lock:
        if _state.get(_COLL_KEY, {}).get("status") == "running":
            return
        _state[_COLL_KEY] = {"status": "running", "started": time.time()}
    threading.Thread(target=_run_collections_install, args=(wanted,), daemon=True).start()


def _run_collections_install(names: list[str]) -> None:
    log = _log_path(_COLL_KEY)
    ok = False
    with log.open("w", buffering=1) as f:
        def emit(m):
            f.write(m if m.endswith("\n") else m + "\n")
            f.flush()
        try:
            ensure_path()
            galaxy = shutil.which("ansible-galaxy")
            if not galaxy:
                raise RuntimeError("ansible-galaxy not found — install Ansible first.")
            emit(f"== Installing {len(names)} collection(s) ==")
            _stream([galaxy, "collection", "install", "--upgrade", *names], emit)
            ok = True
            emit("\n== done — collections installed ==")
        except Exception as e:  # noqa: BLE001 — surface any failure into the log
            emit(f"\n!! install failed: {e}")
    with _lock:
        _state[_COLL_KEY] = {"status": "done" if ok else "failed"}


# ------------------------------------------------------------------ internals
def _stream(cmd, emit) -> None:
    emit("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for line in proc.stdout:
        emit(line.rstrip("\n"))
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"command failed (exit {rc})")


def _ensure_venv(emit) -> Path:
    v = _venv_dir()
    py = v / "bin" / "python"
    if not py.exists():
        emit(f"-- creating engine virtualenv at {v} --")
        subprocess.run([sys.executable, "-m", "venv", str(v)], check=True)
    return py


def _install_pip(engine: str, emit) -> None:
    pkg = "ansible" if engine == "ansible" else "salt"
    py = _ensure_venv(emit)
    _stream([str(py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], emit)
    # --prefer-binary avoids source builds where a wheel exists. Salt still pulls a
    # C-extension dep (timelib) that has no universal wheel; if there's no compiler
    # the build fails, so translate that into an actionable message.
    try:
        _stream([str(py), "-m", "pip", "install", "--prefer-binary", pkg], emit)
    except RuntimeError:
        if engine == "salt" and not (shutil.which("gcc") or shutil.which("cc")):
            emit("")
            emit("!! Salt needs a C toolchain to build a dependency (timelib), and none was found.")
            emit("   Fix ONE of these, then retry:")
            emit("     • Debian/Ubuntu:  sudo apt-get install -y gcc python3-dev libffi-dev")
            emit("     • RHEL/Rocky:     sudo dnf install -y gcc python3-devel libffi-devel")
            emit("     • Or run the container image, which bakes salt-ssh in already.")
        raise


def _install_terraform(emit) -> None:
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), "amd64")
    url = (f"https://releases.hashicorp.com/terraform/{TERRAFORM_VERSION}/"
           f"terraform_{TERRAFORM_VERSION}_linux_{arch}.zip")
    bindir = _bin_dir()
    bindir.mkdir(parents=True, exist_ok=True)
    zpath = bindir / "terraform.zip"
    emit(f"-- downloading {url} --")
    urllib.request.urlretrieve(url, zpath)  # noqa: S310 — fixed HashiCorp release URL
    with zipfile.ZipFile(zpath) as z:
        z.extract("terraform", bindir)
    (bindir / "terraform").chmod(0o755)
    zpath.unlink(missing_ok=True)
    emit(f"-- installed terraform {TERRAFORM_VERSION} to {bindir} --")


def _run_install(engine: str) -> None:
    log = _log_path(engine)
    ok = False
    with log.open("w", buffering=1) as f:
        def emit(m):
            f.write(m if m.endswith("\n") else m + "\n")
            f.flush()
        try:
            emit(f"== Installing {ENGINES[engine]['label']} ==")
            if engine == "terraform":
                _install_terraform(emit)
            else:
                _install_pip(engine, emit)
            ensure_path()
            ok = is_installed(engine)
            done = f"done — {ENGINES[engine]['binary']} is now available"
            emit(f"\n== {done if ok else 'finished, but the binary was not found on PATH'} ==")
        except Exception as e:  # noqa: BLE001 — surface any failure into the log
            emit(f"\n!! install failed: {e}")
    with _lock:
        _state[engine] = {"status": "done" if ok else "failed"}
