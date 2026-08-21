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

        env = _common.credential_env(credential, os.environ)
        env.setdefault("TF_IN_AUTOMATION", "1")

        var_args = []
        for k, v in extra_vars.items():
            var_args += ["-var", f"{k}={v}"]

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
            return _common.stream(cmd, workdir, env, log)

        rc = run_action(upgrade=False)

        # Self-heal a stale provider lock: if the action failed with schema-
        # mismatch errors and a lock file is present, re-init with -upgrade (which
        # re-resolves the lock against the config's version constraints) and retry
        # once. This is what makes a `destroy` work again after a provider whose
        # schema drifted from the config got pinned in .terraform.lock.hcl.
        if rc != 0 and (workdir / ".terraform.lock.hcl").exists():
            try:
                tail = log_path.read_text()[-8000:]
            except OSError:
                tail = ""
            if any(sig in tail for sig in _SCHEMA_MISMATCH):
                emit("\n-- provider schema mismatch detected (stale .terraform.lock.hcl?).")
                emit(f"-- re-resolving providers with `{tool} init -upgrade` and retrying {action} --\n")
                rc = run_action(upgrade=True)

        emit(f"\n== finished: exit code {rc} ==")
        db.set_run_status(run_id, "success" if rc == 0 else "failed",
                          exit_code=rc, finished=int(time.time()))
