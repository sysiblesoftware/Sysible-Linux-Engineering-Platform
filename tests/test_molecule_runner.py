"""Molecule: test a role before it touches sixteen hosts.

The whole design decision here is the DRIVER. Molecule's default driver creates
containers, and SLEP runs inside a container with no Docker socket. Mounting one
would hand root on the host to the service whose entire job is executing
operator-supplied playbooks — SLOP made the opposite call for exactly this
reason, keeping the socket in the tiny updater so the IdP never holds it.

So SLEP runs the `delegated` driver: molecule converges against a real host out
of the inventory SLEP already renders, which needs no new privilege.

A scenario asking for docker/podman is refused WITH THE REASON, before it starts.
The alternative is a run that fails several minutes in with "Cannot connect to
the Docker daemon", which reads as a broken platform rather than a deliberate
boundary — and sends someone looking for a bug that is a decision.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.runners import molecule_runner as mol  # noqa: E402


def scenario(root: Path, name: str, driver: str | None = "delegated") -> Path:
    d = root / "molecule" / name
    d.mkdir(parents=True, exist_ok=True)
    body = "" if driver is None else f"driver:\n  name: {driver}\n"
    (d / "molecule.yml").write_text(body + "platforms:\n  - name: instance\n")
    return d


# ---- the driver boundary ----------------------------------------------------
@pytest.mark.parametrize("driver", ["docker", "podman", "Docker"])
def test_a_container_driver_is_refused_with_the_reason(driver):
    why = mol.driver_refusal(driver)
    assert why, f"{driver} was accepted"
    assert "container" in why
    assert "root on the host" in why, "the refusal does not say WHY it is refused"
    assert "delegated" in why, "the refusal does not say what to do instead"


@pytest.mark.parametrize("driver", ["delegated", "default", "", None])
def test_the_drivers_slep_can_run_are_accepted(driver):
    assert mol.driver_refusal(driver) == ""


def test_an_unknown_driver_is_refused_too():
    """vagrant, ec2, openstack — all create instances SLEP cannot create."""
    why = mol.driver_refusal("vagrant")
    assert "delegated" in why and "vagrant" in why


def test_the_refusal_happens_before_the_run_starts(tmp_path, monkeypatch):
    """Not several minutes in with 'Cannot connect to the Docker daemon', which
    looks like a broken platform instead of a boundary."""
    import backend.db as db
    root = tmp_path / "proj"
    scenario(root, "default", driver="docker")
    ran = []
    monkeypatch.setattr(mol._common, "stream", lambda *a, **k: ran.append(a) or 0)
    monkeypatch.setattr(mol.db, "get_run", lambda rid: {
        "id": rid, "project_id": 1, "target": "default"})
    monkeypatch.setattr(mol.db, "get_project", lambda pid: {"name": "P"})
    monkeypatch.setattr(mol.db, "project_dir", lambda pid: root)
    monkeypatch.setattr(mol.db, "run_log_path", lambda rid: tmp_path / "log.txt")
    status = {}
    monkeypatch.setattr(mol.db, "set_run_status",
                        lambda rid, st, **kw: status.update({"status": st, **kw}))

    mol.launch(1)

    assert ran == [], "molecule was started for a driver SLEP cannot run"
    assert status["status"] == "failed"
    log = (tmp_path / "log.txt").read_text()
    assert "root on the host" in log, "the operator was not told why"


# ---- listing what a project has ---------------------------------------------
def test_scenarios_are_listed_with_whether_they_can_run(tmp_path):
    root = tmp_path / "proj"
    scenario(root, "default", "delegated")
    scenario(root, "containerised", "docker")
    found = {s["name"]: s for s in mol.scenarios(root)}
    assert found["default"]["runnable"] is True
    assert found["containerised"]["runnable"] is False
    assert "container" in found["containerised"]["reason"]


def test_a_project_with_no_molecule_directory_lists_nothing(tmp_path):
    assert mol.scenarios(tmp_path) == []


def test_a_directory_without_a_molecule_yml_is_not_a_scenario(tmp_path):
    (tmp_path / "molecule" / "notascenario").mkdir(parents=True)
    assert mol.scenarios(tmp_path) == []


def test_a_broken_molecule_yml_is_listed_as_unrunnable_not_crashed(tmp_path):
    d = tmp_path / "molecule" / "broken"
    d.mkdir(parents=True)
    (d / "molecule.yml").write_text("driver: [unclosed\n")
    s = mol.scenarios(tmp_path)[0]
    assert s["runnable"] is False and s["reason"]


# ---- the scenario name becomes a path, so it is checked ----------------------
@pytest.mark.parametrize("bad", ["../../etc", "a/b", "", ".", "-x", "a b", "a;b"])
def test_a_scenario_name_that_is_not_one_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        mol.scenario_dir(tmp_path, bad)


def test_an_ordinary_scenario_name_resolves_inside_the_project(tmp_path):
    d = mol.scenario_dir(tmp_path, "default")
    assert str(d).startswith(str(tmp_path.resolve()))
    assert d.name == "default"


def test_the_name_is_validated_before_it_reaches_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(mol.shutil, "which", lambda n: "/usr/bin/molecule")
    with pytest.raises(ValueError):
        mol.build_command(tmp_path, "../../../etc")
    cmd = mol.build_command(tmp_path, "default")
    assert cmd[1:] == ["test", "--scenario-name", "default"]


def test_no_molecule_installed_is_a_message_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(mol.shutil, "which", lambda n: None)
    with pytest.raises(FileNotFoundError):
        mol.build_command(tmp_path, "default")


# ---- it uses the project's own content --------------------------------------
def test_the_projects_roles_and_collections_are_on_the_path(tmp_path, monkeypatch):
    """The content sync installs into the project. A scenario that depends on one
    of those roles must find it without a second global install."""
    root = tmp_path / "proj"
    scenario(root, "default", "delegated")
    seen = {}

    def fake_stream(cmd, cwd, env, log, **kw):
        seen.update({"cmd": cmd, "cwd": cwd, "env": env})
        return 0

    monkeypatch.setattr(mol._common, "stream", fake_stream)
    monkeypatch.setattr(mol._common, "is_stopped", lambda rid: False)
    monkeypatch.setattr(mol.shutil, "which", lambda n: "/usr/bin/molecule")
    monkeypatch.setattr(mol.db, "get_run", lambda rid: {
        "id": rid, "project_id": 1, "target": "default"})
    monkeypatch.setattr(mol.db, "get_project", lambda pid: {"name": "P"})
    monkeypatch.setattr(mol.db, "project_dir", lambda pid: root)
    monkeypatch.setattr(mol.db, "run_log_path", lambda rid: tmp_path / "log.txt")
    monkeypatch.setattr(mol.db, "set_run_status", lambda *a, **k: None)

    mol.launch(1)

    assert str(root / "roles") in seen["env"]["ANSIBLE_ROLES_PATH"]
    assert str(root / "collections") in seen["env"]["ANSIBLE_COLLECTIONS_PATH"]
    assert seen["cwd"] == root, "molecule ran outside the project"


def test_a_missing_scenario_says_how_to_make_one(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(mol.db, "get_run", lambda rid: {
        "id": rid, "project_id": 1, "target": "nope"})
    monkeypatch.setattr(mol.db, "get_project", lambda pid: {"name": "P"})
    monkeypatch.setattr(mol.db, "project_dir", lambda pid: root)
    monkeypatch.setattr(mol.db, "run_log_path", lambda rid: tmp_path / "log.txt")
    monkeypatch.setattr(mol.db, "set_run_status", lambda *a, **k: None)

    mol.launch(1)

    log = (tmp_path / "log.txt").read_text()
    assert "molecule init scenario nope" in log


# ---- and it is a first-class run kind ---------------------------------------
def test_molecule_is_a_registered_runner():
    from backend.app import RUNNERS
    assert "molecule" in RUNNERS


def test_the_console_can_list_a_projects_scenarios(client, project):
    import backend.db as db
    root = db.project_dir(project["id"])
    scenario(root, "default", "delegated")
    scenario(root, "in-docker", "docker")
    rows = client.get(f"/projects/{project['id']}/molecule").json()["scenarios"]
    by = {r["name"]: r for r in rows}
    assert by["default"]["runnable"] is True
    assert by["in-docker"]["runnable"] is False and by["in-docker"]["reason"]
