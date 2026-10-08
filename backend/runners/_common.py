"""Shared runner plumbing — stream a subprocess to a run log, and parse a cloud
credential's secret into environment variables. Keeps the per-engine runners
(terraform, salt, …) short and consistent with the Ansible one.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading

# ---- run cancellation -------------------------------------------------------
# A run executes on a background thread and shells out to one or more child
# processes (terraform init→apply, ansible-playbook, salt-call). To make a run
# stoppable we keep a registry of the child currently streaming for each run,
# plus a set of run ids a stop was requested for. `request_stop` kills the live
# child (its process group, so provider/ssh grandchildren die too) and flags the
# run so the next step doesn't start; runners check `is_stopped` to record the
# terminal status as "canceled" rather than "failed".
_PROCS: dict[int, subprocess.Popen] = {}
_STOP: set[int] = set()
_reg_lock = threading.Lock()


def register(run_id, proc) -> None:
    if run_id is None:
        return
    with _reg_lock:
        _PROCS[run_id] = proc


def unregister(run_id) -> None:
    if run_id is None:
        return
    with _reg_lock:
        _PROCS.pop(run_id, None)


def _kill(proc) -> None:
    """Terminate a child and any process group it leads (SIGTERM). Falls back to a
    plain terminate() if the process-group signal isn't available."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001 — pid gone, no pgid, or non-POSIX
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def request_stop(run_id) -> bool:
    """Flag a run for cancellation and kill its currently-streaming child, if any.
    Returns True if a live child was signalled (so the runner will finalise the
    status), False if nothing was running yet (caller should mark it canceled)."""
    with _reg_lock:
        _STOP.add(run_id)
        proc = _PROCS.get(run_id)
    if proc is not None:
        _kill(proc)
        return True
    return False


def is_stopped(run_id) -> bool:
    with _reg_lock:
        return run_id in _STOP


def clear_stop(run_id) -> None:
    with _reg_lock:
        _STOP.discard(run_id)


def mask(s: str, redact=()) -> str:
    """Replace each secret VALUE in `s` with ***. Used for both the echoed command
    and every streamed output line (a playbook/terraform/salt task can print a secret
    it was given, and the run log is readable by any authenticated viewer)."""
    for r in redact:
        r = str(r)
        if len(r) >= 3:                 # don't mask trivially-short/empty values
            s = s.replace(r, "***")
    return s


def shown_cmd(cmd, redact=()) -> str:
    """Render a command line for the run log with secret substrings masked. Run
    logs are readable by any authenticated user (viewers included), so values
    passed inline (e.g. `-var k=secret` / `-e k=secret` / salt `k=secret`) must not
    be echoed verbatim. `redact` is the list of secret VALUE strings to mask."""
    return mask(" ".join(cmd), redact)


def stream(cmd, cwd, env, log, redact=(), run_id=None) -> int:
    """Run `cmd` in `cwd`, streaming combined stdout/stderr into the open `log`
    file. Returns the exit code (127 if the binary isn't installed). `redact` masks
    secret values in the echoed command line (see shown_cmd). When `run_id` is
    given the child is registered so a concurrent stop can terminate it, and a run
    already flagged for stop is skipped before launching the next step."""
    if run_id is not None and is_stopped(run_id):
        log.write("!! canceled — skipping remaining steps.\n")
        log.flush()
        return 130
    log.write(f"$ {shown_cmd(cmd, redact)}\n")
    log.flush()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )
    except FileNotFoundError:
        log.write(f"!! `{cmd[0]}` is not installed on the SLEP host.\n")
        log.flush()
        return 127
    register(run_id, proc)
    try:
        for line in proc.stdout:
            # Redact secret values the child may echo (e.g. a debug task, a rendered
            # template, terraform plan output) before they hit the viewer-readable log.
            log.write(mask(line, redact) if redact else line)
            log.flush()
        return proc.wait()
    finally:
        unregister(run_id)


def secret_values(credential=None, extra_vars=None) -> list:
    """Every secret value this run hands to the tool, for `redact`.

    Run logs are readable by any authenticated viewer, and each runner injects
    values the viewer is not entitled to see: extra_vars, the KEY=VALUE pairs a
    cloud/env credential puts in the environment (AWS_SECRET_ACCESS_KEY,
    ARM_CLIENT_SECRET, ...), and an ssh_password, which ansible and salt write
    into the inventory/roster in plaintext. A tool echoes those back more readily
    than it looks: `terraform plan` prints resource attributes, ansible -vvv
    prints its inventory, and any task that fails quotes what it was given.

    Each runner was assembling its own list, and each list was the extra_vars and
    nothing else, so the credential values were never masked anywhere. One helper
    so the three cannot drift apart again.

    A private key is NOT here. Keys go to a 0600 file and are passed by path, so
    they are never in-band, and a PEM is line-wrapped — masking it would need to
    match per line, which is a different mechanism from this one.
    """
    out = []
    for v in (extra_vars or {}).values():
        if str(v):
            out.append(str(v))
    if credential and credential.get("secret"):
        kind = credential.get("kind")
        if kind in ("cloud", "env"):
            # Only the VALUES. The names are not secret and masking them would
            # make the log unreadable: "***=***" tells nobody anything.
            injected = credential_env(credential, {})
            out.extend(str(v) for v in injected.values() if str(v))
        elif kind == "ssh_password":
            out.append(str(credential["secret"]))
    return [v for v in out if len(v) >= 3]


def credential_env(credential, base_env) -> dict:
    """Merge a 'cloud' credential's secret (KEY=VALUE lines) into a copy of
    base_env. Blank lines and #comments are ignored. Non-cloud creds are a no-op
    (SSH creds are handled by the engine that needs a key/roster)."""
    env = dict(base_env)
    if not credential or not credential.get("secret"):
        return env
    if credential.get("kind") not in ("cloud", "env"):
        return env
    for raw in credential["secret"].splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env
