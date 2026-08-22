"""Shared runner plumbing — stream a subprocess to a run log, and parse a cloud
credential's secret into environment variables. Keeps the per-engine runners
(terraform, salt, …) short and consistent with the Ansible one.
"""
from __future__ import annotations

import subprocess


def shown_cmd(cmd, redact=()) -> str:
    """Render a command line for the run log with secret substrings masked. Run
    logs are readable by any authenticated user (viewers included), so values
    passed inline (e.g. `-var k=secret` / `-e k=secret` / salt `k=secret`) must not
    be echoed verbatim. `redact` is the list of secret VALUE strings to mask."""
    s = " ".join(cmd)
    for r in redact:
        r = str(r)
        if len(r) >= 3:                 # don't mask trivially-short/empty values
            s = s.replace(r, "***")
    return s


def stream(cmd, cwd, env, log, redact=()) -> int:
    """Run `cmd` in `cwd`, streaming combined stdout/stderr into the open `log`
    file. Returns the exit code (127 if the binary isn't installed). `redact` masks
    secret values in the echoed command line (see shown_cmd)."""
    log.write(f"$ {shown_cmd(cmd, redact)}\n")
    log.flush()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError:
        log.write(f"!! `{cmd[0]}` is not installed on the SLEP host.\n")
        log.flush()
        return 127
    for line in proc.stdout:
        log.write(line)
        log.flush()
    return proc.wait()


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
