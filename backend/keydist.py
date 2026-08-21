"""Distribute SLEP's SSH key to a fleet.

SLEP runs reach hosts over SSH. Password auth works, but keeping a password in a
credential is clumsy and less safe than a key. This module lets the operator, in
one action, push SLEP's own public key onto a set of hosts — authenticating that
one time with the host password (through the inventory's jump host if it has one)
— and then auto-creates a key credential so every future run is key-based.

Mechanics, deliberately simple and portable:
  * A single SLEP-managed ed25519 keypair lives at DATA_DIR/ssh/ (0600). It is
    generated once and reused, so every host trusts the same key.
  * For each host we open one SSH session (sshpass feeds the password via the
    SSHPASS env var, never argv) and append the public key to the host user's
    ~/.ssh/authorized_keys — idempotently (grep -qxF guard), creating ~/.ssh with
    umask 077. No sudo needed: it's the login user's own home.
  * On success we upsert a credential ("SLEP managed key") holding the private
    key, so the operator can pick it for runs immediately.

Progress streams to DATA_DIR/keydist/<inventory_id>.log, tailed by the console
exactly like an engine-install or run log.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import db

_CRED_NAME = "SLEP managed key"
_lock = threading.Lock()
_state: dict[int, dict] = {}   # inventory_id -> {status, started}


def _ssh_dir() -> Path:
    d = db.DATA_DIR / "ssh"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _key_paths() -> tuple[Path, Path]:
    d = _ssh_dir()
    return d / "slep_ed25519", d / "slep_ed25519.pub"


def ensure_key() -> str:
    """Generate the SLEP managed keypair if absent; return the public key text."""
    priv, pub = _key_paths()
    if not priv.exists() or not pub.exists():
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            raise RuntimeError("ssh-keygen is not available in this image.")
        # -N '' → no passphrase (unattended runs); -C labels the key.
        subprocess.run([keygen, "-t", "ed25519", "-N", "", "-C", "slep-managed",
                        "-f", str(priv)], capture_output=True, text=True, check=True)
        try:
            os.chmod(priv, 0o600)
        except OSError:
            pass
    return pub.read_text().strip()


def public_key() -> str:
    priv, pub = _key_paths()
    return pub.read_text().strip() if pub.exists() else ""


def _log_path(key) -> Path:
    d = db.DATA_DIR / "keydist"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.log"


def job_log(key, offset: int = 0):
    p = _log_path(key)
    if not p.exists():
        return "", 0
    data = p.read_bytes()
    return data[offset:].decode("utf-8", "replace"), len(data)


def job_running(key) -> bool:
    return _state.get(key, {}).get("status") == "running"


# Back-compat names for the distribute job (keyed by the inventory id).
def distribute_log(inventory_id: int, offset: int = 0):
    return job_log(inventory_id, offset)


def is_running(inventory_id: int) -> bool:
    return job_running(inventory_id)


def start_distribute(inventory_id: int, host_names, username: str, password: str, bastion: str = ""):
    """Kick off key distribution on a background thread. `host_names` restricts to
    a selection (None/empty = every host in the inventory)."""
    if not username:
        raise ValueError("An SSH username is required.")
    if not password:
        raise ValueError("The host password is required to install the key the first time.")
    inv = db.get_inventory(inventory_id)
    if not inv:
        raise ValueError("Inventory not found.")
    with _lock:
        if is_running(inventory_id):
            return
        _state[inventory_id] = {"status": "running", "started": time.time()}
    threading.Thread(target=_run_distribute,
                     args=(inventory_id, set(host_names or []), username, password, bastion or (inv.get("bastion") or "")),
                     daemon=True).start()


# Operator-directed connections (the operator typed the address + password), so
# host-key checking is disabled on BOTH hops — mirroring the runner's own policy.
# Storing keys would fail anyway (throwaway container, ProxyJump's two unknown
# hosts) and surface as "Host key verification failed".
_HOSTKEY = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]


def _pw_proxy(bastion: str) -> str:
    """A ProxyCommand that logs into the jump host with the SAME password: its own
    `sshpass -e` reads SSHPASS from the inherited env, so the bastion password
    prompt is answered too. This is what makes a two-password-hop path work — one
    sshpass per hop, not one sshpass for both."""
    return ("sshpass -e ssh " + " ".join(_HOSTKEY)
            + f" -o PubkeyAuthentication=no -o ConnectTimeout=15 -W %h:%p {bastion}")


def _pw_cmd(sshpass: str, bastion: str, target: str, remote: str) -> list[str]:
    """Password SSH to `target` (through `bastion` if set), forcing the password
    leg. SSHPASS must be in the env for both the outer call and the proxy."""
    opts = [*_HOSTKEY, "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=15"]
    if bastion:
        opts += ["-o", f"ProxyCommand={_pw_proxy(bastion)}"]
    return [sshpass, "-e", "ssh", *opts, target, remote]


def _key_cmd(bastion: str, keyfile: str, target: str, remote: str) -> list[str]:
    """Key SSH to `target` (through `bastion` if set). ProxyJump reuses the same
    identity for the jump hop. BatchMode so a wrong key fails fast."""
    opts = [*_HOSTKEY, "-o", "ConnectTimeout=15", "-i", keyfile, "-o", "BatchMode=yes"]
    if bastion:
        opts += ["-o", f"ProxyJump={bastion}"]
    return ["ssh", *opts, target, remote]


def _err_line(r) -> str:
    """The most informative one-line reason from a failed SSH — last non-empty
    stderr/stdout line, or the exit code when the process said nothing."""
    lines = [ln for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else f"ssh exited {r.returncode}"


def _install_cmd(pubkey: str) -> str:
    # Append the key idempotently to the login user's authorized_keys.
    safe = pubkey.replace("'", "'\\''")
    return ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
            f"grep -qxF '{safe}' ~/.ssh/authorized_keys || echo '{safe}' >> ~/.ssh/authorized_keys; "
            "echo SLEP_KEY_OK")


def _run_distribute(inventory_id: int, only: set, username: str, password: str, bastion: str):
    log = _log_path(inventory_id)
    ok_hosts, fail_hosts = [], []
    with log.open("w", buffering=1) as f:
        def emit(m):
            f.write(m if m.endswith("\n") else m + "\n"); f.flush()
        try:
            pubkey = ensure_key()
            hosts = db.list_hosts(inventory_id)
            if only:
                hosts = [h for h in hosts if h["name"] in only]
            if not hosts:
                emit("!! No hosts selected — nothing to do."); return
            emit(f"== Distributing SLEP key to {len(hosts)} host(s) as {username}"
                 + (f" via jump host {bastion}" if bastion else "") + " ==")
            emit(f"-- key: {pubkey.split()[0]} …{pubkey.split()[-1][-12:]}")
            emit("")
            sshpass = shutil.which("sshpass")
            if not sshpass:
                emit("!! sshpass is not available in this image — cannot feed the password."); return
            remote = _install_cmd(pubkey)
            env = dict(os.environ, SSHPASS=password)
            for h in hosts:
                target = f"{username}@{h['address']}"
                emit(f"→ {h['name']} ({target}) …")
                cmd = _pw_cmd(sshpass, bastion, target, remote)
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
                except subprocess.TimeoutExpired:
                    emit(f"   ✗ timed out connecting to {h['name']}"); fail_hosts.append(h["name"]); continue
                if r.returncode == 0 and "SLEP_KEY_OK" in r.stdout:
                    emit(f"   ✓ key installed on {h['name']}"); ok_hosts.append(h["name"])
                else:
                    emit(f"   ✗ {h['name']}: {_err_line(r)}")
                    fail_hosts.append(h["name"])
            emit("")
            if ok_hosts:
                cid = db.upsert_credential(_CRED_NAME, kind="ssh", username=username,
                                           secret=_key_paths()[0].read_text())
                emit(f"== {len(ok_hosts)} succeeded, {len(fail_hosts)} failed ==")
                emit(f"Credential “{_CRED_NAME}” (id {cid}) is ready — pick it for key-based runs.")
            else:
                emit(f"== 0 succeeded, {len(fail_hosts)} failed — no credential created ==")
        except Exception as e:  # noqa: BLE001 — surface any failure into the log
            emit(f"!! key distribution failed: {e}")
        finally:
            with _lock:
                _state[inventory_id] = {"status": "done" if ok_hosts else "failed",
                                        "ok": len(ok_hosts), "failed": len(fail_hosts)}


# ------------------------------------------------------------------ prepare a jump host
def start_prepare_bastion(inventory_id: int, bastion: str, password: str):
    """Install SLEP's key on the inventory's jump host itself (direct password
    auth), so the ProxyJump hop is key-based like the targets. `bastion` is a
    user@host spec."""
    bastion = (bastion or "").strip()
    if "@" not in bastion:
        raise ValueError("Jump host must be user@host so SLEP knows who to log in as.")
    if not password:
        raise ValueError("The jump host password is required to install the key the first time.")
    key = f"{inventory_id}-bastion"
    with _lock:
        if job_running(key):
            return
        _state[key] = {"status": "running", "started": time.time()}
    threading.Thread(target=_run_prepare_bastion, args=(key, bastion, password), daemon=True).start()


def _run_prepare_bastion(key: str, bastion: str, password: str):
    log = _log_path(key)
    ok = False
    with log.open("w", buffering=1) as f:
        def emit(m):
            f.write(m if m.endswith("\n") else m + "\n"); f.flush()
        try:
            pubkey = ensure_key()
            sshpass = shutil.which("sshpass")
            if not sshpass:
                emit("!! sshpass is not available in this image — cannot feed the password."); return
            emit(f"== Preparing jump host {bastion} ==")
            emit(f"-- installing SLEP key: {pubkey.split()[0]} …{pubkey.split()[-1][-12:]}")
            emit("")
            emit(f"→ {bastion} …")
            # Direct connection to the bastion (no ProxyJump), password → sshpass.
            cmd = _pw_cmd(sshpass, "", bastion, _install_cmd(pubkey))
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, SSHPASS=password))
            if r.returncode == 0 and "SLEP_KEY_OK" in r.stdout:
                ok = True
                emit(f"   ✓ SLEP key installed on {bastion}")
                emit("")
                emit("== Jump host ready — runs and key distribution now hop through it with the key ==")
            else:
                emit(f"   ✗ {bastion}: {_err_line(r)}")
        except subprocess.TimeoutExpired:
            emit(f"   ✗ timed out connecting to {bastion}")
        except Exception as e:  # noqa: BLE001
            emit(f"!! preparing the jump host failed: {e}")
        finally:
            with _lock:
                _state[key] = {"status": "done" if ok else "failed"}


# ------------------------------------------------------------------ connection test
def _auth_for(credential):
    """Resolve a credential to an SSH auth method. Returns (kind, keyfile, env,
    cleanup): kind is 'password' or 'key'. A password credential feeds SSHPASS; a
    key credential (or the SLEP managed key when credential is None) yields an
    identity file to use with -i."""
    import tempfile
    if credential and credential.get("kind") == "ssh_password" and credential.get("secret"):
        if not shutil.which("sshpass"):
            raise RuntimeError("sshpass is not available for password auth.")
        return ("password", None, {"SSHPASS": credential["secret"]}, lambda: None)
    secret = (credential or {}).get("secret") if credential else None
    if not secret:                                   # SLEP managed key
        if not _key_paths()[0].exists():
            ensure_key()
        return ("key", str(_key_paths()[0]), {}, lambda: None)
    fd, keyfile = tempfile.mkstemp(prefix="slep-probe-")
    with os.fdopen(fd, "w") as kf:
        kf.write(secret if secret.endswith("\n") else secret + "\n")
    os.chmod(keyfile, 0o600)
    return ("key", keyfile, {}, lambda: os.unlink(keyfile))


def start_test(inventory_id: int, host_names, credential_id):
    inv = db.get_inventory(inventory_id)
    if not inv:
        raise ValueError("Inventory not found.")
    key = f"{inventory_id}-test"
    with _lock:
        if job_running(key):
            return
        _state[key] = {"status": "running", "started": time.time()}
    threading.Thread(target=_run_test, args=(key, inventory_id, set(host_names or []),
                                             credential_id, inv.get("bastion") or ""), daemon=True).start()


def _run_test(key: str, inventory_id: int, only: set, credential_id, bastion: str):
    log = _log_path(key)
    reachable, unreachable = [], []
    cleanup = lambda: None
    with log.open("w", buffering=1) as f:
        def emit(m):
            f.write(m if m.endswith("\n") else m + "\n"); f.flush()
        try:
            credential = db.get_credential(int(credential_id), include_secret=True) if credential_id else None
            conn_user = (credential or {}).get("username") or ""
            kind, keyfile, env_extra, cleanup = _auth_for(credential)
            hosts = db.list_hosts(inventory_id)
            if only:
                hosts = [h for h in hosts if h["name"] in only]
            if not hosts:
                emit("!! No hosts selected — nothing to test."); return
            cred_label = credential["name"] if credential else "SLEP managed key"
            emit(f"== Testing SSH to {len(hosts)} host(s) with “{cred_label}”"
                 + (f" via jump host {bastion}" if bastion else "") + " ==")
            emit("")
            env = dict(os.environ, **env_extra)
            sshpass = shutil.which("sshpass")
            for h in hosts:
                user = conn_user or (h.get("variables") or {}).get("ansible_user") or ""
                target = (f"{user}@" if user else "") + h["address"]
                if kind == "password":
                    cmd = _pw_cmd(sshpass, bastion, target, "echo SLEP_CONN_OK")
                else:
                    cmd = _key_cmd(bastion, keyfile, target, "echo SLEP_CONN_OK")
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                except subprocess.TimeoutExpired:
                    emit(f"   ✗ {h['name']} ({target}) — timed out"); unreachable.append(h["name"]); continue
                if r.returncode == 0 and "SLEP_CONN_OK" in r.stdout:
                    emit(f"   ✓ {h['name']} ({target}) — reachable"); reachable.append(h["name"])
                else:
                    emit(f"   ✗ {h['name']} ({target}) — {_err_line(r)}")
                    unreachable.append(h["name"])
            emit("")
            emit(f"== {len(reachable)} reachable, {len(unreachable)} unreachable ==")
            if unreachable and not reachable:
                emit("Tip: if these want a password, run “Distribute SSH key” first, then test with the SLEP managed key.")
        except Exception as e:  # noqa: BLE001
            emit(f"!! connection test failed: {e}")
        finally:
            try:
                cleanup()
            except OSError:
                pass
            with _lock:
                _state[key] = {"status": "done", "ok": len(reachable), "failed": len(unreachable)}
