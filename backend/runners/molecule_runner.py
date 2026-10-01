"""Molecule runner — test a role before it touches the fleet.

Molecule is the step between "I wrote a role" and "I ran it on sixteen hosts":
it converges the role against a throwaway instance, asserts the result, and
checks that a second converge changes nothing.

THE DRIVER IS THE WHOLE DESIGN DECISION. Molecule's default driver creates
containers, and SLEP runs inside a container with no Docker socket — mounting
one would hand root on the host to a service whose entire job is executing
operator-supplied playbooks. SLOP made the opposite call for exactly this reason
(the updater holds the socket so the IdP does not). So SLEP runs the `delegated`
driver by default: Molecule converges against a real host out of the inventory
SLEP already renders, which needs no new privilege at all and reuses the thing
SLEP is best at.

A scenario that asks for the docker/podman driver is REFUSED, with the reason —
rather than being run and failing several minutes later with "Cannot connect to
the Docker daemon", which looks like a broken platform instead of a deliberate
boundary.

The run's `target` is the scenario name; empty means `default`.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import yaml

from .. import db
from . import _common

# Scenario names are directory names under molecule/. Strict, because the value
# becomes a path segment and `--scenario-name ../../etc` is not a scenario.
SCENARIO_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
DEFAULT_SCENARIO = "default"

# Drivers that need a container runtime this service deliberately cannot reach.
CONTAINER_DRIVERS = ("docker", "podman", "containers")
# What SLEP supports: converge against a host, not a container it cannot create.
SUPPORTED_DRIVERS = ("delegated", "default")


def scenarios(root: Path) -> list[dict]:
    """The scenarios this project defines, and whether SLEP can run each."""
    out = []
    base = root / "molecule"
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        cfg = d / "molecule.yml"
        if not cfg.is_file():
            continue
        driver, err = "", ""
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            driver = str((data.get("driver") or {}).get("name") or "")
        except Exception as e:  # noqa: BLE001
            err = str(e)
        out.append({"name": d.name, "driver": driver or "(unset)",
                    "runnable": not err and driver_refusal(driver) == "",
                    "reason": err or driver_refusal(driver)})
    return out


def driver_refusal(driver: str) -> str:
    """Why SLEP will not run this driver, or "". """
    d = (driver or "").strip().lower()
    if not d or d in SUPPORTED_DRIVERS:
        return ""
    if d in CONTAINER_DRIVERS:
        return (f"this scenario uses the '{d}' driver, which creates containers. "
                f"SLEP has no access to a container runtime — giving the service "
                f"that runs your playbooks a Docker socket would hand it root on "
                f"the host. Use 'driver: delegated' and point the scenario at a "
                f"scratch host, or run molecule from a shell on that host.")
    return (f"SLEP runs molecule's 'delegated' driver; this scenario asks for "
            f"'{d}'. Change the driver, or run it from a shell.")


def scenario_dir(root: Path, name: str) -> Path:
    """The scenario's directory, having proved it is inside the project."""
    if not SCENARIO_RE.match(name or ""):
        raise ValueError(f"'{name}' is not a scenario name")
    d = (root / "molecule" / name).resolve()
    # Belt and braces: the regex already forbids separators, but a project dir
    # reached through a symlink would otherwise let `resolve()` land outside.
    if not str(d).startswith(str(root.resolve())):
        raise ValueError("that scenario is outside the project")
    return d


def build_command(root: Path, scenario: str, action: str = "test") -> list[str]:
    molecule = shutil.which("molecule")
    if not molecule:
        raise FileNotFoundError("molecule")
    scenario_dir(root, scenario)           # validate before it reaches argv
    return [molecule, action, "--scenario-name", scenario]


def launch(run_id: int) -> None:
    run = db.get_run(run_id)
    if not run:
        return
    project = db.get_project(run["project_id"])
    root = db.project_dir(run["project_id"])
    scenario = (run.get("target") or "").strip() or DEFAULT_SCENARIO
    log_path = db.run_log_path(run_id)
    db.set_run_status(run_id, "running", started=int(time.time()))

    with open(log_path, "a", encoding="utf-8") as log:
        def emit(line=""):
            log.write(line if line.endswith("\n") else line + "\n")
            log.flush()

        try:
            d = scenario_dir(root, scenario)
        except ValueError as e:
            emit(f"!! {e}")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return
        cfg = d / "molecule.yml"
        if not cfg.is_file():
            emit(f"!! No molecule/{scenario}/molecule.yml in this project.")
            emit("-- create one with:  molecule init scenario " + scenario)
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return

        driver = ""
        try:
            driver = str((yaml.safe_load(cfg.read_text(encoding="utf-8")) or {})
                         .get("driver", {}).get("name") or "")
        except Exception as e:  # noqa: BLE001
            emit(f"!! molecule/{scenario}/molecule.yml could not be read: {e}")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return

        # REFUSED BEFORE IT STARTS, not several minutes in with "Cannot connect to
        # the Docker daemon" — which reads as a broken platform rather than a
        # deliberate boundary.
        refusal = driver_refusal(driver)
        if refusal:
            emit(f"!! {refusal}")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return

        emit(f"== SLEP molecule · project '{(project or {}).get('name')}' · "
             f"scenario '{scenario}' ==")
        emit(f"-- driver: {driver or 'delegated (default)'}\n")

        env = dict(os.environ)
        # The project's own roles/collections, installed by the content sync, so a
        # scenario can depend on them without a second global install.
        env["ANSIBLE_ROLES_PATH"] = os.pathsep.join(
            [str(root / "roles"), env.get("ANSIBLE_ROLES_PATH", "")]).rstrip(os.pathsep)
        env["ANSIBLE_COLLECTIONS_PATH"] = os.pathsep.join(
            [str(root / "collections"), env.get("ANSIBLE_COLLECTIONS_PATH", "")]
        ).rstrip(os.pathsep)
        env.setdefault("ANSIBLE_FORCE_COLOR", "1")

        try:
            cmd = build_command(root, scenario)
        except FileNotFoundError:
            emit("!! `molecule` is not installed on the SLEP host.")
            emit("-- install it into the engine venv:  pip install molecule")
            db.set_run_status(run_id, "failed", exit_code=127, finished=int(time.time()))
            return

        rc = _common.stream(cmd, root, env, log, run_id=run_id)
        if _common.is_stopped(run_id):
            _common.clear_stop(run_id)
            emit("\n== canceled by operator ==")
            db.set_run_status(run_id, "canceled", exit_code=rc or 130,
                              finished=int(time.time()))
            return
        emit(f"\n== finished: exit code {rc} ==")
        db.set_run_status(run_id, "success" if rc == 0 else "failed",
                          exit_code=rc, finished=int(time.time()))
