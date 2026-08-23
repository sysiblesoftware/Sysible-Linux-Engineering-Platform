"""Terraform / OpenTofu runner — run `<tool> <action>` against a project's .tf.

The run's `target` is the action: `plan`, `apply`, or `destroy`. State lives in
the project workdir (terraform.tfstate), so plan/apply/destroy over the same
project share state the way a normal checkout does. Provider credentials come
from an attached 'cloud' credential whose secret is KEY=VALUE env lines (e.g.
AWS_ACCESS_KEY_ID=…), injected into the run environment. extra_vars become
`-var key=value`.

The CLI can be Terraform (`terraform`) or OpenTofu (`tofu`) — OpenTofu is a
drop-in, CLI-compatible fork, so the same project runs under either. The tool is
chosen per run (stash_tool), or via the SLEP_TF_TOOL env, else auto-detected.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .. import db
from . import _common

ACTIONS = {"plan", "apply", "destroy"}

# Transient per-run tool choice ('terraform' | 'tofu'), set just before launch()
# and consumed once inside it. Same-process dict, mirrors the Ansible runner.
_TOOL: dict[int, str] = {}


def stash_tool(run_id: int, tool: str) -> None:
    tool = (tool or "").strip().lower()
    if tool:
        _TOOL[run_id] = tool


def pop_tool(run_id: int) -> str:
    return _TOOL.pop(run_id, "")


# Provider/lock mismatch signatures. When a stale .terraform.lock.hcl pins a
# provider version whose schema no longer matches the config, every argument
# reads as "Unsupported argument" / "Missing required argument". Re-initialising
# with -upgrade re-resolves the lock to the config's version constraints.
_SCHEMA_MISMATCH = (
    "Unsupported argument",
    "Missing required argument",
    "but no definition was found",
    "does not match configured",
    "no suitable version",
    "Failed to query available provider packages",
)


def _resolve_tool(choice: str) -> str:
    """Pick the CLI: explicit per-run choice → SLEP_TF_TOOL env → auto-detect
    (prefer terraform, fall back to tofu). Either satisfies the same config."""
    choice = (choice or os.environ.get("SLEP_TF_TOOL", "")).strip().lower()

    def have(b):
        return shutil.which(b) is not None

    if choice in ("tofu", "opentofu"):
        return "tofu" if have("tofu") else "terraform"
    if choice in ("terraform", "tf"):
        return "terraform" if have("terraform") else ("tofu" if have("tofu") else "terraform")
    return "terraform" if have("terraform") else ("tofu" if have("tofu") else "terraform")


def launch(run_id: int) -> None:
    run = db.get_run(run_id)
    if not run:
        return
    log_path = db.run_log_path(run_id)
    project = db.get_project(run["project_id"])
    workdir = db.project_dir(run["project_id"])
    credential = (
        db.get_credential(run["credential_id"], include_secret=True)
        if run.get("credential_id") else None
    )
    try:
        extra_vars = json.loads(run.get("extra_vars") or "{}")
    except (TypeError, ValueError):
        extra_vars = {}
    action = (run["target"] or "").strip().lower()
    tool = _resolve_tool(pop_tool(run_id))
    name = "OpenTofu" if tool == "tofu" else "Terraform"

    db.set_run_status(run_id, "running", started=int(time.time()))
    with log_path.open("w", buffering=1) as log:
        def emit(m):
            log.write(m if m.endswith("\n") else m + "\n")

        emit(f"== SLEP run #{run_id} · project '{project['name']}' · {tool} {action} ==")
        if action not in ACTIONS:
            emit(f"!! Unknown {name} action '{action}'. Use plan, apply, or destroy.")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return
        if not any(workdir.glob("*.tf")):
            emit(f"!! No .tf files in this project — nothing for {name} to do.")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return

        # Preflight the cloud-init ISO tool. The dmacvicar/libvirt provider builds
        # each VM's cloud-init ISO by shelling out to `mkisofs` (or genisoimage /
        # xorriso). If it's missing, apply fails *after* the slow base-image
        # download — so on apply, when the config uses libvirt_cloudinit_disk,
        # check up front and fail fast with a clear, actionable message.
        if action == "apply":
            tf_text = ""
            for f in workdir.glob("*.tf"):
                try:
                    tf_text += f.read_text()
                except OSError:
                    pass
            if "libvirt_cloudinit_disk" in tf_text and not any(
                    shutil.which(b) for b in ("mkisofs", "genisoimage", "xorriso")):
                emit("!! Missing cloud-init ISO tool. The libvirt provider builds each VM's")
                emit("!! cloud-init ISO with `mkisofs` (or genisoimage / xorriso), which isn't on")
                emit("!! PATH in the SLEP container. Install genisoimage (the image bakes it in as")
                emit("!! of this release — run `slep update`), then re-apply. Failing now so you")
                emit("!! don't wait on the base-image download first.")
                db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
                return

        env = _common.credential_env(credential, os.environ)
        env.setdefault("TF_IN_AUTOMATION", "1")

        var_args = []
        for k, v in extra_vars.items():
            var_args += ["-var", f"{k}={v}"]
        # Secret -var values must not be echoed into the (viewer-readable) run log.
        redact = [str(v) for v in extra_vars.values() if str(v)]

        def run_action(upgrade: bool) -> int:
            init = [tool, "init", "-input=false", "-no-color"]
            if upgrade:
                init.append("-upgrade")
            rc = _common.stream(init, workdir, env, log)
            if rc != 0:
                emit(f"\n== {tool} init failed: exit {rc} ==")
                return rc
            if action == "plan":
                cmd = [tool, "plan", "-input=false", "-no-color", *var_args]
            elif action == "apply":
                cmd = [tool, "apply", "-input=false", "-auto-approve", "-no-color", *var_args]
            else:  # destroy
                cmd = [tool, "destroy", "-input=false", "-auto-approve", "-no-color", *var_args]
            return _common.stream(cmd, workdir, env, log, redact=redact)

        def _mismatch() -> bool:
            try:
                tail = log_path.read_text()[-8000:]
            except OSError:
                tail = ""
            return any(sig in tail for sig in _SCHEMA_MISMATCH)

        rc = run_action(upgrade=False)

        # Self-heal tier 1 — stale provider lock: if the action failed with schema-
        # mismatch errors and a lock file is present, re-init with -upgrade (which
        # re-resolves the lock against the config's version constraints) and retry
        # once. This is what makes a `destroy` work again after a provider whose
        # schema drifted from the config got pinned in .terraform.lock.hcl.
        if rc != 0 and (workdir / ".terraform.lock.hcl").exists() and _mismatch():
            emit("\n-- provider schema mismatch detected (stale .terraform.lock.hcl?).")
            emit(f"-- re-resolving providers with `{tool} init -upgrade` and retrying {action} --\n")
            rc = run_action(upgrade=True)

        # Self-heal tier 2 — poisoned provider cache: a corrupt/stub provider plugin
        # in the project's local .terraform cache survives `-upgrade` (it still
        # satisfies the version constraint, so init reports "using previously-
        # installed …" and reuses it). Wipe the .terraform dir + lock and re-init
        # from scratch so a clean provider is downloaded, then retry once. State
        # (terraform.tfstate) is deliberately left untouched.
        if rc != 0 and _mismatch():
            emit("\n-- still schema-mismatched after -upgrade — the cached provider plugin looks corrupt.")
            emit("-- clearing the local provider cache (.terraform + lock) and re-initialising clean --\n")
            shutil.rmtree(workdir / ".terraform", ignore_errors=True)
            (workdir / ".terraform.lock.hcl").unlink(missing_ok=True)
            rc = run_action(upgrade=False)
            if rc != 0 and _mismatch():
                emit("\n-- STILL schema-mismatched after a clean re-download. This is a provider")
                emit("-- major-version schema change, not a bad download: the config's HCL syntax")
                emit("-- doesn't match the resolved provider version. Pin the provider to a compatible")
                emit("-- version in required_providers (e.g. a tighter version constraint), then apply.")
                emit("-- (dmacvicar/libvirt 0.9.x rewrote its resource schema vs 0.7/0.8.) --")

        # After a successful apply of a Create-Infrastructure project, read the new
        # VMs into the project's own inventory automatically — so they're immediately
        # targetable by Ansible/Salt with no manual "→ Inventory" step. Best-effort:
        # a non-infra project or a not-yet-ready output just skips silently.
        if action == "apply" and rc == 0:
            try:
                from .. import app as _app
                built = _app._autobuild_infra_inventory(run["project_id"], run=run)
                if built:
                    iid, iname, n = built
                    if n > 0:
                        emit(f"\n-- SLEP: added {n} VM(s) to inventory “{iname}” (#{iid}) — "
                             f"the next Ansible/Salt step will target them.")
                    else:
                        # VMs exist in the output but none had a usable address —
                        # almost always libvirt IPs not assigned yet (DHCP / guest
                        # agent). Say so, since an empty inventory otherwise fails
                        # the next step with a confusing "nothing to target".
                        emit(f"\n-- SLEP: inventory “{iname}” (#{iid}) got 0 hosts — the VMs "
                             f"applied but have no IP yet (libvirt assigns addresses via "
                             f"DHCP/guest-agent after boot). Wait for them to come up, then "
                             f"re-run the Ansible/Salt step (or the '→ Inventory' action).")
            except Exception:  # noqa: BLE001 — never fail the apply over this
                pass

        emit(f"\n== finished: exit code {rc} ==")
        db.set_run_status(run_id, "success" if rc == 0 else "failed",
                          exit_code=rc, finished=int(time.time()))
