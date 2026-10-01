"""Ansible runner — turn a SLEP run row into an actual `ansible-playbook`
invocation and stream its output to the run log.

Design goals (the "friendlier than AAP" part):
  * No execution environment, no container per run — just run `ansible-playbook`
    from the project's own directory so roles/relative paths/`ansible.cfg` all
    resolve the way the author expects.
  * The inventory is RENDERED from the SLEP database into a throwaway INI file,
    so what runs always matches what the console shows.
  * Credentials never touch the browser or the project dir: the SSH key/password
    is written to a 0600 file in a per-run temp dir and removed when the run ends.

`launch(run_id)` blocks until the run finishes; the API layer calls it on a
background thread and the console tails the log file.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .. import db, keydist, projcfg, vault
from . import _common

# Transient per-run sudo passwords (never persisted): set just before launch(),
# consumed once inside it. Keyed by run id; same process, so a plain dict is fine.
_BECOME: dict[int, str] = {}


def stash_become(run_id: int, password: str) -> None:
    if password:
        _BECOME[run_id] = password


def pop_become(run_id: int) -> str:
    return _BECOME.pop(run_id, "")


# Transient per-run CLI options (--limit, --start-at-task), same pattern as above.
_RUNOPTS: dict[int, dict] = {}


def stash_opts(run_id: int, opts: dict) -> None:
    opts = {k: v for k, v in (opts or {}).items() if v}
    if opts:
        _RUNOPTS[run_id] = opts


def pop_opts(run_id: int) -> dict:
    return _RUNOPTS.pop(run_id, {})


# Survey password answers. Same transient treatment as the become password: they
# reach the play as extra vars through a 0600 file, and are never written to the
# run row — so a re-run repeats everything except the secret, and asks for that
# again. A secret persisted on a run is readable by anyone who can list runs.
_SECRET_VARS: dict[int, dict] = {}


def stash_secret_vars(run_id: int, values: dict) -> None:
    values = {k: v for k, v in (values or {}).items() if v not in (None, "")}
    if values:
        _SECRET_VARS[run_id] = values


def pop_secret_vars(run_id: int) -> dict:
    return _SECRET_VARS.pop(run_id, {})


# ---------------------------------------------------------------------------
# Job options — the set AAP exposes on a job template, translated to argv.
#
# SLEP had --limit and --start-at-task and nothing else, which left three things
# an operator reaches for constantly out of the console: tags, a dry run, and
# --force-handlers. The last one matters more than it looks: a handler only runs
# at the END of a play, so a playbook that fails after notifying one leaves the
# service un-restarted, and the RE-RUN does not notify again (the task is already
# in the desired state). Without --force-handlers the only way out is to make a
# cosmetic change to force the notify — which is exactly the kind of thing people
# do at 2am and regret.
#
# Kept as a pure function: it is the part worth testing, and it needs no fleet.
VERBOSITY_MAX = 4
# Ansible tag names: identifiers, optionally comma-separated. Deliberately strict
# — a typo here silently selects NO tasks and the run "succeeds" having done
# nothing, which is the worst failure shape available.
_TAGS_RE = re.compile(r"\A[A-Za-z0-9_.:@+-]+(?:\s*,\s*[A-Za-z0-9_.:@+-]+)*\Z")


def build_options(opts: dict) -> tuple[list[str], list[str], str]:
    """(argv, notes-for-the-log, error). A non-empty error refuses the run.

    Nothing here is interpolated into a shell, and every value lands as its own
    argv element after its flag — so a value can never be read as another flag.
    The validation is about catching a mistake early, not about escaping.
    """
    argv: list[str] = []
    notes: list[str] = []
    opts = opts or {}

    limit = str(opts.get("limit") or "").strip()
    if limit:
        argv += ["--limit", limit]
        notes.append(f"limited to: {limit}")

    start_at = str(opts.get("start_at_task") or "").strip()
    if start_at:
        argv += ["--start-at-task", start_at]
        notes.append(f"starting at task: {start_at}")

    for key, flag in (("tags", "--tags"), ("skip_tags", "--skip-tags")):
        val = str(opts.get(key) or "").strip()
        if not val:
            continue
        if not _TAGS_RE.match(val):
            return [], [], (f"{flag} takes comma-separated tag names "
                            f"(letters, digits, _ . : @ + -); got {val!r}")
        val = ",".join(t.strip() for t in val.split(","))
        argv += [flag, val]
        notes.append(f"{flag[2:]}: {val}")

    if opts.get("check"):
        argv.append("--check")
        notes.append("CHECK MODE — nothing will be changed")
    if opts.get("diff"):
        argv.append("--diff")
        notes.append("showing diffs")
    if opts.get("force_handlers"):
        argv.append("--force-handlers")
        notes.append("handlers will run even if the play fails")

    verbosity = opts.get("verbosity")
    if verbosity not in (None, "", 0, "0"):
        try:
            v = int(verbosity)
        except (TypeError, ValueError):
            return [], [], f"verbosity must be 0-{VERBOSITY_MAX}; got {verbosity!r}"
        if not 0 <= v <= VERBOSITY_MAX:
            return [], [], f"verbosity must be 0-{VERBOSITY_MAX}; got {v}"
        if v:
            argv.append("-" + "v" * v)
            notes.append(f"verbosity: -{'v' * v}")

    if opts.get("idempotence"):
        # NOT an argv flag — it is a second pass, run after the first succeeds.
        if opts.get("check"):
            return [], [], ("an idempotence check cannot run in check mode: a check "
                            "run changes nothing, so a second pass proves nothing")
        if start_at:
            return [], [], ("an idempotence check cannot start at a task: the second "
                            "pass would skip the tasks whose idempotence is in question")
        notes.append("idempotence check: the playbook will run a SECOND time, "
                     "and must report no changes")

    return argv, notes, ""


# The PLAY RECAP is the only place Ansible states how many things it changed.
_RECAP_CHANGED = re.compile(r"\bchanged=(\d+)")


def changed_in_recap(output: str) -> int | None:
    """Total `changed=` across the hosts in the LAST play recap, or None.

    None means there was no recap at all — the run died before finishing — which
    is a different thing from "changed nothing" and must not be read as success.
    Only text after the final `PLAY RECAP` is considered: `changed=` also appears
    in per-task output under high verbosity, and counting those would make every
    run look non-idempotent.
    """
    idx = output.rfind("PLAY RECAP")
    if idx < 0:
        return None
    total = 0
    found = False
    for line in output[idx:].splitlines()[1:]:
        m = _RECAP_CHANGED.search(line)
        if m:
            total += int(m.group(1))
            found = True
    return total if found else None


def _ansible_group(name: str) -> str:
    """Ansible INI group names allow only letters, digits and underscores — a
    Controller environment like "Sysible Labs" (with a space) would otherwise
    write an invalid `[Sysible Labs]` section and the whole inventory fails to
    parse. Map every other character to '_'."""
    g = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    if g and g[0].isdigit():
        g = "g_" + g            # groups can't start with a digit
    return g or "ungrouped"


# Render-time injection guards (defence-in-depth; the API validates on input too).
_RE_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)$")
_RE_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_RE_VAL = re.compile(r"^[A-Za-z0-9_.:/@=+-]+$")   # safe as a bare INI host-var token


def _ini_safe_host(s) -> bool:
    return bool(s) and not str(s).startswith("-") and bool(_RE_HOST.fullmatch(str(s)))


def _ini_safe_user(s) -> bool:
    return bool(s) and not str(s).startswith("-") and bool(_RE_USER.fullmatch(str(s)))


def _ini_safe_val(s) -> bool:
    return bool(_RE_VAL.fullmatch(str(s)))


def _render_inventory(hosts, credential, dest: Path, bastion: str = "", bastion_key: str = "") -> None:
    """Write an Ansible INI inventory from SLEP hosts. Hosts are grouped by their
    comma-separated `groups`; every host also lands in the implicit `all`. SSH
    connection vars come from the attached credential."""
    # group name -> list of "hostline"
    groups: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    conn_user = (credential or {}).get("username") or ""

    bhost = keydist.bastion_host(bastion) if bastion else ""
    # Hosts that ARE the jump host must connect directly — jumping through
    # themselves closes the connection ("Connection closed by UNKNOWN").
    direct_names: list[str] = []

    cuser = conn_user if _ini_safe_user(conn_user) else ""
    for h in hosts:
        # Defence-in-depth: never emit an address/user/var that could inject an INI
        # token (extra ansible_ssh_common_args → ProxyCommand → RCE). Inputs are
        # validated at the API boundary; this guards already-stored/imported data too.
        addr = h["address"]
        if not _ini_safe_host(addr):
            continue   # unsafe address — skip rather than risk an injected connection var
        parts = [h["name"], f"ansible_host={addr}"]
        if cuser:
            parts.append(f"ansible_user={cuser}")
        # ssh_password credentials pass the password as a host var (server-side
        # only — never rendered into the console). Key creds use --private-key.
        if credential and credential.get("kind") == "ssh_password" and credential.get("secret"):
            parts.append(f"ansible_password={credential['secret']}")
            parts.append(f"ansible_become_password={credential['secret']}")
        for k, v in (h.get("variables") or {}).items():
            if not re.fullmatch(r"[A-Za-z0-9_]+", str(k)):
                continue
            vs = v if isinstance(v, str) else json.dumps(v)
            if not _ini_safe_val(vs):
                continue   # a value with whitespace/quotes can't sit safely on an INI host line
            parts.append(f"{k}={vs}")
        line = " ".join(parts)
        if bhost and h["address"] == bhost:
            direct_names.append(h["name"])
        gs = [_ansible_group(g) for g in (h.get("groups") or "").split(",") if g.strip()]
        if gs:
            for g in gs:
                groups.setdefault(g, []).append(line)
        else:
            ungrouped.append(line)

    common = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    with dest.open("w") as f:
        for line in ungrouped:
            f.write(line + "\n")
        for g, lines in groups.items():
            f.write(f"\n[{g}]\n")
            for line in lines:
                f.write(line + "\n")
        # Optional SSH jump host (bastion): reach every host through it. Native
        # ProxyJump doesn't reliably pass StrictHostKeyChecking=no down to the jump
        # sub-connection ("Connection closed by UNKNOWN"/"Host key verification
        # failed"), so spell the jump out as an explicit ProxyCommand that carries
        # the host-key options and keys into the bastion with SLEP's managed key
        # (which 'Prepare jump host' / 'Distribute SSH key' installed there).
        if bastion:
            f.write("\n[all:vars]\n")
            # ssh runs the ProxyCommand via /bin/sh, so shell-quote the bastion (and the
            # key path) — the API already charset-validates a bastion, this is the last
            # line of defence so a value that ever slipped the checks can't break out of
            # the ssh option. A validated bastion has no special chars → quote is a no-op.
            if bastion_key:
                proxy = f"ssh {common} -o BatchMode=yes -i {shlex.quote(str(bastion_key))} -W %h:%p {shlex.quote(bastion)}"
                f.write(f'ansible_ssh_common_args=-o ProxyCommand="{proxy}" {common}\n')
            else:
                f.write(f"ansible_ssh_common_args=-o ProxyJump={shlex.quote(bastion)} {common}\n")
            # A target that IS the jump host connects directly: a dedicated group
            # whose vars override [all:vars] (more specific than the 'all' group).
            if direct_names:
                f.write("\n[slep_direct]\n")
                for n in direct_names:
                    f.write(n + "\n")
                f.write("\n[slep_direct:vars]\n")
                f.write(f"ansible_ssh_common_args={common}\n")


def _emit_unreachable_help(emit, has_bastion: bool, proxy_hop_closed: bool,
                           auth_denied: bool, timed_out: bool) -> None:
    """Translate an all-UNREACHABLE recap into plain next steps. Ansible's raw
    'Connection closed by UNKNOWN port 65535' is opaque: with a ProxyJump, UNKNOWN
    means SSH got PAST the jump host and the onward hop to the target closed."""
    emit("")
    emit("!! Hosts were UNREACHABLE — SSH couldn't connect. This is a connectivity/")
    emit("   credential issue on the target side, not a playbook error. Likely causes:")
    if auth_denied:
        emit("   • Auth was refused — the selected credential's user/key isn't accepted")
        emit("     on the target. Check the SSH credential and that its key is authorized.")
    if proxy_hop_closed:
        emit("   • 'Connection closed by UNKNOWN' = the jump host connected but the hop to")
        emit("     the target closed. The bastion can reach itself but not the target:")
        emit("       – WRONG JUMP HOST: for libvirt/cloud VMs on a private NAT network, the jump")
        emit("         host must be the HYPERVISOR that runs them — it's the only machine on their")
        emit("         network. A sibling box on the LAN can reach itself but not the VMs. Set the")
        emit("         inventory's jump host to the hypervisor (the user@host from its qemu+ssh URI);")
        emit("         SLEP now does this automatically for VMs it builds, or")
        emit("       – the target has no SSH server running (Sysible Linux ships SSH OFF by")
        emit("         default — enable sshd on the host, or manage it via its agent), or")
        emit("       – the bastion's sshd has 'AllowTcpForwarding no'.")
    if timed_out:
        emit("   • The connection TIMED OUT — SSH never reached the host's port 22. This is")
        emit("     NOT a wrong key/user (that would say 'Permission denied'). It means one of:")
        emit("       – the VMs were still booting when this ran. Freshly-applied cloud-init VMs")
        emit("         take ~30–90s before sshd answers — just re-run the Ansible step, or run")
        emit("         the whole cadence again; SLEP now waits for :22 before configuring.")
        emit("       – SLEP has no network route to the VMs' subnet. libvirt VMs sit on a NAT")
        emit("         network (e.g. 192.168.x) that a SLEP *container* can't reach unless it")
        emit("         shares the host network or a route is added. Verify from the SLEP host:")
        emit("           nc -vz <target-ip> 22     (or)   ssh <user>@<target-ip>")
        emit("         If that also hangs, it's routing/firewall — not SLEP or the playbook.")
    if not (auth_denied or proxy_hop_closed or timed_out):
        emit("   • The target isn't accepting SSH: sshd not running (Sysible Linux ships SSH")
        emit("     OFF by default), wrong port, or a firewall in the way.")
    if has_bastion:
        emit("   • Verify the jump host manually:")
        emit("       ssh -J <user>@<bastion> <user>@<target>")
    else:
        emit("   • Verify SSH manually from the SLEP host:  ssh <user>@<target>")
    emit("   Note: agent-enrolled hosts are managed by the Controller's agent (outbound")
    emit("   poll) — Ansible needs INBOUND SSH to them, which is a separate path.")


def _wait_for_ssh(hosts, emit, timeout: int = 120, bastion: str = "") -> None:
    """Give freshly-applied VMs a chance to finish booting before handing off to
    Ansible, so a still-booting host is a short wait instead of an instant
    UNREACHABLE. TCP-probes each host's port 22 directly; if all answer on the
    first pass there's no delay at all. Skipped when a jump host is configured —
    the real path then runs through the bastion, which this direct probe can't
    model. Never fails the run: after the deadline it just proceeds (Ansible then
    reports the real outcome, and the UNREACHABLE help explains a persistent one)."""
    if bastion:
        return
    pending = [(h["name"], h["address"]) for h in hosts if h.get("address")]
    if not pending:
        return
    deadline = time.time() + timeout
    waited = False
    while pending and time.time() < deadline:
        still = []
        for nm, addr in pending:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            try:
                s.connect((addr, 22))
            except OSError:
                still.append((nm, addr))
            finally:
                try:
                    s.close()
                except OSError:
                    pass
        if not still:
            break
        pending = still
        waited = True
        emit(f"-- waiting for SSH (:22) on {len(pending)} host(s) to come up: "
             f"{', '.join(a for _, a in pending)} …")
        time.sleep(6)
    if waited and not pending:
        emit("-- all hosts now answer on :22 — proceeding.\n")
    elif pending:
        emit(f"-- {len(pending)} host(s) still silent on :22 after {timeout}s: "
             f"{', '.join(a for _, a in pending)}. Proceeding — Ansible will report the outcome.\n")


def _write_key(secret: str, dest: Path) -> None:
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode())
        if not secret.endswith("\n"):
            os.write(fd, b"\n")
    finally:
        os.close(fd)


def _key_loads(path: Path) -> bool:
    """True if OpenSSH can actually parse this private key. A malformed/encrypted/
    truncated key produces ssh's 'Load key … error in libcrypto' at connect time and
    then a misleading 'Permission denied' — catching it here lets the runner say so
    plainly and fall back to SLEP's managed key instead of dead-ending the run."""
    kg = shutil.which("ssh-keygen")
    if not kg:
        return True   # can't check here; assume ok and let ssh decide
    try:
        r = subprocess.run([kg, "-y", "-f", str(path)], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return True


def launch(run_id: int) -> None:
    run = db.get_run(run_id)
    if not run:
        return
    log_path = db.run_log_path(run_id)
    project = db.get_project(run["project_id"])
    workdir = db.project_dir(run["project_id"])
    inventory = db.get_inventory(run["inventory_id"]) if run.get("inventory_id") else None
    hosts = db.list_hosts(run["inventory_id"]) if run.get("inventory_id") else []
    bastion = (inventory or {}).get("bastion") or ""
    # Fall back to the project's hypervisor jump host when the inventory itself has
    # none — infra VMs live on the hypervisor's private network, unreachable
    # directly. Covers inventories built before the jump host was known.
    if not bastion and run.get("project_id"):
        try:
            from .. import app as _app
            bastion = _app._project_hypervisor_bastion(run["project_id"])
        except Exception:  # noqa: BLE001
            bastion = ""
    credential = (
        db.get_credential(run["credential_id"], include_secret=True)
        if run.get("credential_id") else None
    )
    try:
        extra_vars = json.loads(run.get("extra_vars") or "{}")
    except (TypeError, ValueError):
        extra_vars = {}

    tmp = Path(tempfile.mkdtemp(prefix=f"slep-run-{run_id}-"))
    inv_file = tmp / "inventory.ini"
    key_file = tmp / "id_key"

    db.set_run_status(run_id, "running", started=int(time.time()))

    with log_path.open("w", buffering=1) as log:
        def emit(msg: str):
            log.write(msg if msg.endswith("\n") else msg + "\n")

        try:
            if not hosts:
                emit("!! No hosts in the selected inventory — nothing to target.")
                raise RuntimeError("empty inventory")

            playbook = (workdir / run["target"]).resolve()
            # Refuse to escape the project dir (the target comes from the console).
            if not str(playbook).startswith(str(workdir.resolve())):
                emit(f"!! Playbook path escapes the project directory: {run['target']}")
                raise RuntimeError("invalid playbook path")
            if not playbook.is_file():
                emit(f"!! Playbook not found: {run['target']}")
                raise RuntimeError("playbook missing")

            # The jump hop authenticates with SLEP's managed key (installed on the
            # bastion by 'Prepare jump host' / 'Distribute SSH key').
            _render_inventory(hosts, credential, inv_file, bastion=bastion,
                              bastion_key=keydist.managed_key_path())

            # Freshly-applied VMs may still be booting — wait for :22 so the cadence
            # (apply → inventory → configure) doesn't race the boot. No-op delay when
            # hosts are already up, or when reached through a jump host.
            _wait_for_ssh(hosts, emit, bastion=bastion)

            cmd = ["ansible-playbook", "-i", str(inv_file), str(playbook)]
            # Private key selection. SLEP's managed key is the ONE key baked into
            # every VM it builds, so for those hosts it's the reliable identity. Use
            # the operator's chosen SSH-key credential when it's actually a valid key;
            # if it's malformed (the classic 'error in libcrypto' → Permission denied)
            # or absent, fall back to the managed key rather than dead-ending.
            managed_key = keydist.managed_key_path()
            is_infra = False
            try:
                is_infra = bool(db.get_infra(run["project_id"]))
            except Exception:  # noqa: BLE001
                is_infra = False
            key_ready = False
            if credential and credential.get("kind") == "ssh" and credential.get("secret"):
                _write_key(credential["secret"], key_file)
                if _key_loads(key_file):
                    cmd += ["--private-key", str(key_file)]
                    key_ready = True
                else:
                    emit(f"!! The SSH private key in credential "
                         f"'{credential.get('name', '?')}' is malformed — OpenSSH can't parse it "
                         f"(encrypted, truncated, or not an OpenSSH/PEM key). Not offering it.")
            if not key_ready and managed_key:
                cmd += ["--private-key", managed_key]
                key_ready = True
                emit("-- using SLEP's managed key (the key baked into VMs SLEP builds).")
            # For infra VMs, also offer the managed key as a secondary identity when a
            # (valid) operator key is primary — so a key that simply doesn't match the
            # VM still gets in via the baked-in managed key. ssh tries each in turn.
            extra_ssh_args = ""
            if is_infra and managed_key and key_ready and "--private-key" in cmd and cmd[cmd.index("--private-key") + 1] != managed_key:
                extra_ssh_args = f"-o IdentityFile={managed_key}"
            # Job options: --limit/--start-at-task/--tags/--skip-tags/--check/
            # --diff/--force-handlers/-v, and the idempotence second pass.
            opts = pop_opts(run_id)
            opt_argv, opt_notes, opt_err = build_options(opts)
            if opt_err:
                emit(f"!! {opt_err}")
                raise RuntimeError(opt_err)
            cmd += opt_argv
            for note in opt_notes:
                emit(f"-- {note}")
            # Extra vars go through a 0600 @file, not `-e k=v` on argv — a value the
            # operator typed into the Variables box may be a secret, and argv is visible
            # in the process list (`ps`) to any local user. Matches the vault/become
            # handling below.
            if extra_vars:
                evfile = tmp / "extravars.json"
                fd = os.open(str(evfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, json.dumps(extra_vars).encode())
                finally:
                    os.close(fd)
                cmd += ["-e", "@" + str(evfile)]

            # Inject the secrets vault as `vault.<name>` via a 0600 vars file
            # (-e @file keeps the values out of the process list / ps). Scope to the
            # PROJECT'S org so a run can never materialise another tenant's secrets.
            _porg = (project or {}).get("org_id")
            secrets = db.all_secret_ciphertexts([_porg] if _porg else None)
            if secrets:
                vfile = tmp / "vault.json"
                fd = os.open(str(vfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, json.dumps({"vault": {n: vault.decrypt(ct) for n, ct in secrets}}).encode())
                finally:
                    os.close(fd)
                cmd += ["-e", "@" + str(vfile)]

            # Survey password answers: 0600 @file, never argv, never the run row.
            survey_secrets = pop_secret_vars(run_id)
            if survey_secrets:
                sfile = tmp / "survey.json"
                fd = os.open(str(sfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, json.dumps(survey_secrets).encode())
                finally:
                    os.close(fd)
                cmd += ["-e", "@" + str(sfile)]

            # Sudo/become password: a per-run override (transient), else the one
            # stored (encrypted) on the credential. Passed via a 0600 vars file so
            # it never lands in the process list. Lets a key credential run `become`
            # tasks against password-sudo hosts (e.g. an admin account).
            # Per-run override (transient), else the credential's stored become
            # password (db returns it decrypted on the include_secret read).
            become_pw = pop_become(run_id) or (credential.get("become_secret") if credential else "") or ""
            if become_pw:
                bfile = tmp / "become.json"
                fd = os.open(str(bfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, json.dumps({"ansible_become_password": become_pw}).encode())
                finally:
                    os.close(fd)
                cmd += ["-e", "@" + str(bfile)]

            env = dict(os.environ)
            # The project's ansible.cfg (edited via the console's "Ansible Config"
            # action) is authoritative: point ansible at it explicitly and let it
            # own every setting it declares.
            if projcfg.exists(run["project_id"]):
                env["ANSIBLE_CONFIG"] = str(projcfg.config_path(run["project_id"]))
            # First-run friendliness: don't wedge on unknown host keys — UNLESS the
            # project's ansible.cfg sets host_key_checking itself (env vars override
            # ansible.cfg, so forcing it here would silently ignore the operator's
            # explicit choice).
            if not projcfg.defines(run["project_id"], "defaults", "host_key_checking"):
                env.setdefault("ANSIBLE_HOST_KEY_CHECKING", "False")
            env.setdefault("ANSIBLE_FORCE_COLOR", "1")
            # Offer SLEP's managed key as an extra ssh identity for infra VMs (see
            # above): appended to every target ssh invocation, so a mismatched
            # operator key still yields to the baked-in managed key.
            if extra_ssh_args:
                env["ANSIBLE_SSH_EXTRA_ARGS"] = (env.get("ANSIBLE_SSH_EXTRA_ARGS", "") + " " + extra_ssh_args).strip()

            # Secret -e values must not be echoed into the (viewer-readable) log.
            emit(f"== SLEP run #{run_id} · project '{project['name']}' ==")
            emit(f"$ {_common.shown_cmd(cmd, [str(v) for v in extra_vars.values() if str(v)])}")
            emit(f"-- inventory: {len(hosts)} host(s); credential: "
                 f"{credential['name'] if credential else 'none'}"
                 f"{'; jump host: ' + bastion if bastion else ''} --\n")
            log.flush()

            if _common.is_stopped(run_id):
                _common.clear_stop(run_id)
                emit("\n== canceled by operator ==")
                db.set_run_status(run_id, "canceled", exit_code=130, finished=int(time.time()))
                return
            proc = subprocess.Popen(
                cmd, cwd=str(workdir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True,
            )
            _common.register(run_id, proc)
            unreachable = proxy_hop_closed = auth_denied = timed_out = False
            try:
                for line in proc.stdout:      # live stream
                    log.write(line)
                    log.flush()
                    if "UNREACHABLE!" in line:
                        unreachable = True
                    if "Connection closed by UNKNOWN" in line:
                        proxy_hop_closed = True
                    if "Permission denied" in line or "Authentication failed" in line:
                        auth_denied = True
                    if "Connection timed out" in line or "Operation timed out" in line:
                        timed_out = True
                rc = proc.wait()
            finally:
                _common.unregister(run_id)

            if _common.is_stopped(run_id):
                _common.clear_stop(run_id)
                emit("\n== canceled by operator ==")
                db.set_run_status(run_id, "canceled", exit_code=rc or 130, finished=int(time.time()))
                return
            emit(f"\n== finished: exit code {rc} ==")
            if unreachable:
                _emit_unreachable_help(emit, bool(bastion), proxy_hop_closed, auth_denied, timed_out)

            # THE IDEMPOTENCE CHECK. A correct playbook describes a desired state,
            # so running it twice changes nothing the second time. Anything that
            # reports `changed` on the second pass is doing work every run — a
            # `command:` with no `creates:`, a template that rewrites a timestamp,
            # a service bounced unconditionally — and on a schedule that is a
            # restart every night that nobody asked for.
            #
            # Only after a clean first pass: a second run on top of a failure is
            # measuring the wrong thing.
            if rc == 0 and opts.get("idempotence"):
                emit("\n== idempotence check: running it a second time ==")
                emit("-- a playbook that describes a desired state changes nothing here.\n")
                log.flush()
                if _common.is_stopped(run_id):
                    _common.clear_stop(run_id)
                    emit("\n== canceled by operator ==")
                    db.set_run_status(run_id, "canceled", exit_code=130, finished=int(time.time()))
                    return
                tail: list[str] = []
                proc2 = subprocess.Popen(
                    cmd, cwd=str(workdir), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                    start_new_session=True,
                )
                _common.register(run_id, proc2)
                try:
                    for line in proc2.stdout:
                        log.write(line)
                        log.flush()
                        # Bounded: only the recap is needed, and a -vvvv run can
                        # pour out megabytes we would otherwise hold in memory.
                        tail.append(line)
                        if len(tail) > 400:
                            del tail[:200]
                    rc2 = proc2.wait()
                finally:
                    _common.unregister(run_id)
                changed = changed_in_recap("".join(tail))
                if rc2 != 0:
                    emit(f"\n!! the second pass itself failed (exit {rc2}) — the playbook "
                         f"is not repeatable on an already-converged host.")
                    rc = rc2
                elif changed is None:
                    emit("\n!! the second pass produced no PLAY RECAP, so there is nothing "
                         "to compare — treating the idempotence check as failed rather "
                         "than assuming it passed.")
                    rc = 1
                elif changed:
                    emit(f"\n!! NOT IDEMPOTENT: the second pass reported {changed} change(s). "
                         f"Something in this playbook does work on every run. Re-run with "
                         f"--diff to see what it rewrites.")
                    rc = 1
                else:
                    emit("\n== idempotent: the second pass changed nothing ==")

            db.set_run_status(
                run_id, "success" if rc == 0 else "failed",
                exit_code=rc, finished=int(time.time()),
            )
        except FileNotFoundError:
            emit("!! `ansible-playbook` is not installed on the SLEP host. "
                 "Install ansible (apt install ansible / pipx install ansible).")
            db.set_run_status(run_id, "failed", exit_code=127, finished=int(time.time()))
        except Exception as e:  # noqa: BLE001 — surface any failure into the log
            emit(f"\n!! run aborted: {e}")
            cur = db.get_run(run_id)
            if cur and cur.get("status") == "running":
                db.set_run_status(run_id, "failed", exit_code=1, finished=int(time.time()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
