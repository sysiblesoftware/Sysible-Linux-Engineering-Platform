"""Terraform runner — run `terraform <action>` against the project's .tf files.

The run's `target` is the action: `plan`, `apply`, or `destroy`. State lives in
the project workdir (terraform.tfstate), so plan/apply/destroy over the same
project share state the way a normal Terraform checkout does. Provider
credentials come from an attached 'cloud' credential whose secret is KEY=VALUE
env lines (e.g. AWS_ACCESS_KEY_ID=…), injected into the run environment.
extra_vars become `-var key=value`.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import db
from . import _common

ACTIONS = {"plan", "apply", "destroy"}


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

    db.set_run_status(run_id, "running", started=int(time.time()))
    with log_path.open("w", buffering=1) as log:
        def emit(m):
            log.write(m if m.endswith("\n") else m + "\n")

        emit(f"== SLEP run #{run_id} · project '{project['name']}' · terraform {action} ==")
        if action not in ACTIONS:
            emit(f"!! Unknown terraform action '{action}'. Use plan, apply, or destroy.")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return
        if not any(workdir.glob("*.tf")):
            emit("!! No .tf files in this project — nothing for Terraform to do.")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return

        env = _common.credential_env(credential, os.environ)
        env.setdefault("TF_IN_AUTOMATION", "1")

        var_args = []
        for k, v in extra_vars.items():
            var_args += ["-var", f"{k}={v}"]

        # init is idempotent and required before plan/apply/destroy.
        rc = _common.stream(["terraform", "init", "-input=false", "-no-color"], workdir, env, log)
        if rc != 0:
            emit(f"\n== terraform init failed: exit {rc} ==")
            db.set_run_status(run_id, "failed", exit_code=rc, finished=int(time.time()))
            return

        if action == "plan":
            cmd = ["terraform", "plan", "-input=false", "-no-color", *var_args]
        elif action == "apply":
            cmd = ["terraform", "apply", "-input=false", "-auto-approve", "-no-color", *var_args]
        else:  # destroy
            cmd = ["terraform", "destroy", "-input=false", "-auto-approve", "-no-color", *var_args]

        rc = _common.stream(cmd, workdir, env, log)
        emit(f"\n== finished: exit code {rc} ==")
        db.set_run_status(run_id, "success" if rc == 0 else "failed",
                          exit_code=rc, finished=int(time.time()))
