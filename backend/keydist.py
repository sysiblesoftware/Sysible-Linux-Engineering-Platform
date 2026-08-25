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


def fingerprint() -> str:
    """SHA256 fingerprint of the on-disk managed public key (`ssh-keygen -lf`), or ''
    when there's no key or ssh-keygen is unavailable. Lets the UI show WHICH key is
    current so a stale one baked into old VMs is recognisable."""
    _priv, pub = _key_paths()
    if not pub.exists() or not shutil.which("ssh-keygen"):
        return ""
    try:
        r = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def remove_key() -> bool:
    """Delete the on-disk managed keypair. Returns True if anything was removed. The
    'SLEP managed key' credential is left to the caller (the API deletes it too on a
    full remove). After this, ensure_key() would mint a fresh, DIFFERENT key."""
    priv, pub = _key_paths()
    removed = False
    for p in (priv, pub):
        try:
            if p.exists():
                p.unlink()
                removed = True
        except OSError:
            pass
    return removed


def regenerate_key() -> str:
    """Replace the managed keypair with a brand-new one and re-sync the credential to
    it. Use this to RESET SLEP's identity when the deployed key drifted from the
    on-disk one (the classic 'none of the keys work any more'). Returns the new
    public key. Callers must then re-install it on hypervisors and re-apply so VMs
    bake in the new key — the old key stops working the moment this runs."""
    remove_key()
    pub = ensure_key()
    sync_managed_credential()
    return pub


def sync_managed_credential() -> bool:
    """Keep the 'SLEP managed key' credential holding the CURRENT on-disk managed
    private key, so a run using it authenticates with exactly the key SLEP bakes
    into the VMs. The two can diverge if the on-disk key was regenerated after the
    credential was first created (e.g. a data dir that didn't persist) — which
    surfaces as 'Permission denied (publickey)' even though the key looks baked in.
    Only refreshes an EXISTING credential (creating one, with its username, is the
    distribute flow's job). Returns True if it changed anything."""
    priv = _key_paths()[0]
    if not priv.exists():
        return False
    try:
        want = priv.read_text()
        for c in db.list_credentials(include_secret=True):
            if c.get("name") == _CRED_NAME:
                if (c.get("secret") or "") != want:
                    db.set_credential_secret(c["id"], want)
                    return True
                return False
    except Exception:  # noqa: BLE001
        pass
    return False


def managed_key_path() -> str:
    """Filesystem path of SLEP's managed PRIVATE key if it exists, else ''. The
    runner uses it to authenticate the jump-host hop (which 'Prepare jump host' /
    'Distribute SSH key' set up with this key)."""
    priv = _key_paths()[0]
    return str(priv) if priv.exists() else ""


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


def bastion_host(bastion: str) -> str:
    """The host part of a user@host[:port] jump spec ('' if none)."""
    b = (bastion or "").split("@", 1)[-1]
    return b.split(":", 1)[0].strip()


def hop_for(bastion: str, target_address: str) -> str:
    """The jump host to reach `target_address` — empty (direct) when the target
    IS the jump host, which would otherwise loop the ProxyJump through itself
    ('Connection closed by UNKNOWN')."""
    return "" if bastion and target_address == bastion_host(bastion) else bastion


def _key_proxy(bastion: str) -> str:
    """An explicit ProxyCommand that logs into the jump host with SLEP's managed
    key. Unlike native ProxyJump, spelling out the jump as a ProxyCommand lets us
    force StrictHostKeyChecking=no on the JUMP hop too (ProxyJump doesn't reliably
    inherit it → 'Host key verification failed'). The bastion must already carry
    the key (distribute/prepare installs it first)."""
    mk = str(_key_paths()[0])
    # Quote the key path: ssh tokenises the ProxyCommand string itself, so a space in
    # DATA_DIR would otherwise split the -i argument and fail the jump login (surfacing
    # as the misleading "tunnel closed before login").
    return ("ssh " + " ".join(_HOSTKEY)
            + f" -o ConnectTimeout=15 -o BatchMode=yes -i '{mk}' -W %h:%p {bastion}")


def _pw_cmd(sshpass: str, bastion: str, target: str, remote: str) -> list[str]:
    """Password SSH to `target`. With a `bastion`, the JUMP hop authenticates with
    SLEP's managed key (via ProxyCommand) and only the target uses the password —
    a single `sshpass` per command. Direct (no bastion) forces the password leg."""
    opts = [*_HOSTKEY, "-o", "ConnectTimeout=15", "-o", "PubkeyAuthentication=no"]
    if bastion:
        opts += ["-o", f"ProxyCommand={_key_proxy(bastion)}"]
    return [sshpass, "-e", "ssh", *opts, target, remote]


def _key_cmd(bastion: str, keyfile: str, target: str, remote: str) -> list[str]:
    """Key SSH to `target` (through `bastion` if set). The jump hop uses SLEP's
    managed key (ProxyCommand); the target uses `keyfile`. BatchMode fails fast."""
    opts = [*_HOSTKEY, "-o", "ConnectTimeout=15", "-i", keyfile, "-o", "BatchMode=yes"]
    if bastion:
        opts += ["-o", f"ProxyCommand={_key_proxy(bastion)}"]
    return ["ssh", *opts, target, remote]


def _friendly_err(raw: str, has_bastion: bool) -> str:
    """Translate a raw SSH failure into a short, plain-English reason."""
    low = (raw or "").lower()
    # A jump-host CHANNEL failure ('channel N: open failed: connect failed /
    # administratively prohibited') is the jump host being unable to reach the target
    # (routing/firewall) — a DIFFERENT fault from the target's sshd being down. Classify
    # it separately so the operator isn't sent to fix the VM when the jump host is the
    # problem. (Plain substring — the old "channel .* open failed" was a regex written as
    # a substring test and never matched.)
    if "open failed" in low or "administratively prohibited" in low:
        if "administratively prohibited" in low:
            return "the jump host refused to forward to the target (its firewall/policy blocked port 22)"
        return "the jump host couldn't reach the target (wrong VM address, or the jump host isn't on the VM's network)"
    if "connection closed by unknown" in low:
        # No target host in the message → the failure was on the JUMP hop itself
        # (its ProxyCommand died), typically because the jump host doesn't trust
        # SLEP's key. Only say "target" when there's no bastion in play.
        return ("couldn't get through the jump host — it may not trust SLEP's key yet "
                "(run “Prepare jump host”), or its address/login is wrong") if has_bastion else \
               "the host closed the connection before login (SSH may be off)"
    if "permission denied" in low:
        return ("reached the target, but login was refused — wrong username/password, or the key "
                "isn't installed") if has_bastion else \
               "login refused — key not installed yet, or wrong username/password"
    if "connection timed out" in low or "operation timed out" in low or "timed out" in low:
        return "no response — host down, firewalled, or wrong address"
    if "connection refused" in low:
        return "connection refused — nothing is listening on SSH (port 22)"
    if "no route to host" in low or "network is unreachable" in low:
        return "no network route to the host"
    if "could not resolve" in low or "name or service not known" in low:
        return "address didn't resolve"
    if "host key verification failed" in low:
        return "host key check failed"
    return (raw or "").strip() or "unknown error"


def _err_line(r, has_bastion: bool = False) -> str:
    """A short, human reason from a failed SSH — the raw last line translated."""
    lines = [ln for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
    raw = lines[-1].strip() if lines else f"ssh exited {r.returncode}"
    return _friendly_err(raw, has_bastion)


def _install_cmd(pubkey: str) -> str:
    # Append the key idempotently to the login user's authorized_keys.
    safe = pubkey.replace("'", "'\\''")
    return ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
            f"grep -qxF '{safe}' ~/.ssh/authorized_keys || echo '{safe}' >> ~/.ssh/authorized_keys; "
            "echo SLEP_KEY_OK")


# A read-only probe run after a successful login: report who we are and whether
# cloud-init is even present on the VM. A missing/failed cloud-init is the usual
# reason a password or key baked into the image never took, so surfacing it turns
# a bare "login refused" into an actionable diagnosis.
PROBE_REMOTE = (
    "printf 'SLEP_AUTH_OK '; id -un 2>/dev/null || whoami 2>/dev/null; "
    "if command -v cloud-init >/dev/null 2>&1; then "
    "printf 'cloud-init: '; (cloud-init status 2>/dev/null | head -1 || echo '(status unavailable)'); "
    "else echo 'cloud-init: not installed'; fi"
)


def probe_cmd(bastion: str, target: str, *, keyfile: str = "", sshpass: str = "") -> list[str]:
    """Build a non-mutating SSH probe (PROBE_REMOTE) to `target` through `bastion`,
    authenticating with a key (`keyfile`) or a password (`sshpass` binary, password
    fed via the SSHPASS env by the caller)."""
    if keyfile:
        return _key_cmd(bastion, keyfile, target, PROBE_REMOTE)
    return _pw_cmd(sshpass, bastion, target, PROBE_REMOTE)


def parse_probe(stdout: str) -> tuple[str, str]:
    """Pull (login_user, cloud_init_line) out of a PROBE_REMOTE stdout."""
    who, ci = "", ""
    for ln in (stdout or "").splitlines():
        s = ln.strip()
        if s.startswith("SLEP_AUTH_OK"):
            who = s[len("SLEP_AUTH_OK"):].strip()
        elif s.lower().startswith("cloud-init"):
            ci = s
    return who, ci


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

            # With a jump host, install the key on the bastion FIRST (one clean
            # password hop) so the target hops can jump through it with the key —
            # a single sshpass per host, which is reliable.
            if bastion:
                emit(f"→ jump host {bastion} (preparing) …")
                try:
                    rb = subprocess.run(_pw_cmd(sshpass, "", bastion, remote),
                                        capture_output=True, text=True, timeout=60, env=env)
                    if rb.returncode == 0 and "SLEP_KEY_OK" in rb.stdout:
                        emit("   ✓ jump host ready — targets will hop through it with the key")
                    else:
                        emit(f"   ✗ jump host {bastion}: {_err_line(rb, has_bastion=False)}")
                        emit("!! Can't reach targets until the jump host accepts the key. Check the "
                             "jump host address / password and try again.")
                        return
                except subprocess.TimeoutExpired:
                    emit(f"   ✗ timed out connecting to jump host {bastion}"); return
                emit("")

            for h in hosts:
                target = f"{username}@{h['address']}"
                hop = hop_for(bastion, h["address"])
                emit(f"→ {h['name']} ({target}){' [direct — is the jump host]' if bastion and not hop else ''} …")
                cmd = _pw_cmd(sshpass, hop, target, remote)
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
                except subprocess.TimeoutExpired:
                    emit(f"   ✗ timed out connecting to {h['name']}"); fail_hosts.append(h["name"]); continue
                if r.returncode == 0 and "SLEP_KEY_OK" in r.stdout:
                    emit(f"   ✓ key installed on {h['name']}"); ok_hosts.append(h["name"])
                else:
                    emit(f"   ✗ {h['name']}: {_err_line(r, has_bastion=bool(hop))}")
                    fail_hosts.append(h["name"])
            emit("")
            if ok_hosts:
                cid = db.upsert_credential(_CRED_NAME, kind="ssh", username=username,
                                           secret=_key_paths()[0].read_text())
                emit(f"== {len(ok_hosts)} succeeded, {len(fail_hosts)} failed ==")
                emit(f"Credential “{_CRED_NAME}” (id {cid}) is ready — pick it for key-based runs.")
            else:
                emit(f"== 0 succeeded, {len(fail_hosts)} failed — no credential created ==")
            # All failed through a jump host → almost always the wrong jump host:
            # the VMs are on the hypervisor's private NAT network, so ONLY the
            # hypervisor can reach them. Name the fix explicitly.
            if bastion and fail_hosts and not ok_hosts:
                emit("")
                emit("Tip: these are on a private network the jump host can't reach. For libvirt/"
                     "cloud VMs, the jump host MUST be the HYPERVISOR that runs them (it's the only "
                     "machine on their NAT network) — set the inventory's jump host to the hypervisor "
                     "(e.g. the user@host from its qemu+ssh URI), not another box on the LAN.")
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
                emit(f"   ✗ {bastion}: {_err_line(r, has_bastion=False)}")
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
                hop = hop_for(bastion, h["address"])   # direct when the target IS the jump host
                if kind == "password":
                    cmd = _pw_cmd(sshpass, hop, target, "echo SLEP_CONN_OK")
                else:
                    cmd = _key_cmd(hop, keyfile, target, "echo SLEP_CONN_OK")
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
                except subprocess.TimeoutExpired:
                    emit(f"   ✗ {h['name']} ({target}) — timed out"); unreachable.append(h["name"]); continue
                if r.returncode == 0 and "SLEP_CONN_OK" in r.stdout:
                    emit(f"   ✓ {h['name']} ({target}) — reachable"); reachable.append(h["name"])
                else:
                    emit(f"   ✗ {h['name']} ({target}) — {_err_line(r, has_bastion=bool(hop))}")
                    unreachable.append(h["name"])
            emit("")
            emit(f"== {len(reachable)} reachable, {len(unreachable)} unreachable ==")
            if unreachable and not reachable:
                if bastion:
                    emit("Tip: if the jump host reached the targets but the tunnel closed, it can't route "
                         "to them. For libvirt/cloud VMs, the jump host must be the HYPERVISOR that runs "
                         "them (only it is on their NAT network) — set the inventory's jump host to the "
                         "hypervisor (the user@host from its qemu+ssh URI).")
                else:
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
