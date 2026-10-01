"""Per-project roles and collections — AAP's project sync.

SLEP could install a fixed list of five collections into the shared engine venv
and that was the whole story: nothing per project, no roles at all, no private
Galaxy. A playbook that depended on a role simply did not run, and the only fix
was a shell on the SLEP host.

The rule that matters most here is WHERE things get installed. Two projects
pinning different versions of the same role is ordinary; installing both into one
shared venv means whichever synced last wins and the other project silently runs
the wrong code — the kind of failure that looks like the playbook being flaky.
`-p <project>/roles` keeps them apart, and ansible-playbook already looks there.

The commands are built as data so they can be asserted without ansible-galaxy
being installed: the -r/-p pairing IS the correctness of this feature.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import galaxy  # noqa: E402


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(galaxy.shutil, "which", lambda n: "/usr/bin/ansible-galaxy")
    return tmp_path


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ---- where it installs ------------------------------------------------------
def test_roles_are_installed_into_the_project_not_the_shared_venv(proj):
    """Two projects pinning different versions of the same role is normal. One
    shared install root means the last sync wins and the other project runs the
    wrong code, which reads as the playbook being flaky."""
    write(proj, "roles/requirements.yml", "- name: geerlingguy.nginx\n")
    cmd = galaxy.sync_commands(proj)[0]
    assert cmd[1:4] == ["role", "install", "-r"]
    assert cmd[cmd.index("-p") + 1] == str(proj / "roles")


def test_collections_go_to_the_projects_own_path(proj):
    write(proj, "collections/requirements.yml", "collections:\n  - community.general\n")
    cmd = galaxy.sync_commands(proj)[0]
    assert cmd[1:3] == ["collection", "install"]
    assert cmd[cmd.index("-p") + 1] == str(proj / "collections")


def test_both_files_mean_both_installs_in_order(proj):
    write(proj, "roles/requirements.yml", "- name: a.b\n")
    write(proj, "collections/requirements.yml", "collections:\n  - c.d\n")
    cmds = galaxy.sync_commands(proj)
    assert [c[1] for c in cmds] == ["role", "collection"]


def test_a_project_with_no_requirements_needs_no_commands(proj):
    assert galaxy.sync_commands(proj) == []


def test_force_is_how_a_changed_version_pin_takes(proj):
    """Without --force, ansible-galaxy leaves an already-installed role alone, so
    editing `version:` in requirements.yml appears to do nothing."""
    write(proj, "roles/requirements.yml", "- name: a.b\n  version: 2.0.0\n")
    assert "--force" not in galaxy.sync_commands(proj)[0]
    assert "--force" in galaxy.sync_commands(proj, force=True)[0]


def test_no_ansible_means_a_message_not_a_traceback(proj, monkeypatch):
    monkeypatch.setattr(galaxy.shutil, "which", lambda n: None)
    write(proj, "roles/requirements.yml", "- name: a.b\n")
    with pytest.raises(galaxy.GalaxyError) as e:
        galaxy.sync_commands(proj)
    assert "install Ansible first" in str(e.value)


# ---- reading what the project asks for --------------------------------------
def test_a_bare_list_is_the_old_roles_format(proj):
    write(proj, "roles/requirements.yml",
          "- name: geerlingguy.nginx\n  version: 3.1.4\n- src: https://x/y.git\n")
    reqs = galaxy.requirements(proj)
    assert reqs["roles"]["path"] == "roles/requirements.yml"
    assert reqs["roles"]["entries"][0] == {"name": "geerlingguy.nginx", "version": "3.1.4"}
    assert reqs["roles"]["entries"][1]["src"] == "https://x/y.git"


def test_a_mapping_carries_both_kinds_in_one_file(proj):
    write(proj, "requirements.yml",
          "roles:\n  - name: a.b\ncollections:\n  - name: community.general\n")
    reqs = galaxy.requirements(proj)
    assert [e["name"] for e in reqs["roles"]["entries"]] == ["a.b"]


def test_a_broken_requirements_file_is_reported_not_raised(proj):
    """A project with a malformed requirements.yml must still open in the
    console, with the reason on screen."""
    write(proj, "roles/requirements.yml", "- name: [unclosed\n")
    reqs = galaxy.requirements(proj)
    assert reqs["errors"], "a parse error vanished"
    assert "roles/requirements.yml" in reqs["errors"][0]


def test_a_project_with_nothing_declares_nothing(proj):
    reqs = galaxy.requirements(proj)
    assert reqs["roles"] is None and reqs["collections"] is None and not reqs["errors"]


# ---- what is actually installed ---------------------------------------------
def test_installed_roles_are_listed(proj):
    (proj / "roles" / "geerlingguy.nginx" / "tasks").mkdir(parents=True)
    (proj / "roles" / "notarole").mkdir(parents=True)
    assert galaxy.installed(proj)["roles"] == ["geerlingguy.nginx"]


def test_installed_collections_are_listed_by_full_name(proj):
    (proj / "collections" / "ansible_collections" / "community" / "general").mkdir(parents=True)
    assert galaxy.installed(proj)["collections"] == ["community.general"]


def test_an_empty_project_has_nothing_installed(proj):
    assert galaxy.installed(proj) == {"roles": [], "collections": []}


# ---- private Galaxy / Automation Hub ----------------------------------------
def test_a_token_never_reaches_the_command_line():
    """argv is visible in `ps` to any local user — the same reason extra-vars go
    through a 0600 file rather than -e on the command line."""
    env = galaxy.server_env(["https://hub.internal/api/galaxy/"], token="s3cret")
    assert any(v == "s3cret" for v in env.values()), "the token never reached the env"
    assert "ANSIBLE_GALAXY_SERVER_LIST" in env


def test_servers_are_ordered_and_named():
    env = galaxy.server_env(["https://a/", "https://b/"], token="t")
    assert env["ANSIBLE_GALAXY_SERVER_LIST"] == "slep1,slep2"
    assert env["ANSIBLE_GALAXY_SERVER_SLEP1_URL"] == "https://a/"
    assert env["ANSIBLE_GALAXY_SERVER_SLEP2_URL"] == "https://b/"


def test_a_token_with_no_server_still_authenticates_to_public_galaxy():
    env = galaxy.server_env([], token="t")
    assert env["ANSIBLE_GALAXY_TOKEN"] == "t"
    assert "ANSIBLE_GALAXY_SERVER_LIST" not in env


def test_no_servers_and_no_token_changes_nothing():
    env = galaxy.server_env([], "")
    assert "ANSIBLE_GALAXY_SERVER_LIST" not in env
    assert "ANSIBLE_GALAXY_TOKEN" not in env


def test_blank_server_entries_are_ignored():
    env = galaxy.server_env(["", "  ", "https://a/"], token="t")
    assert env["ANSIBLE_GALAXY_SERVER_LIST"] == "slep1"


# ---- the sync itself, with a stub ansible-galaxy -----------------------------
def test_sync_runs_each_install_and_streams_it(proj, monkeypatch, tmp_path):
    stub = tmp_path / "bin" / "ansible-galaxy"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/bin/sh\necho \"installing $*\"\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setattr(galaxy.shutil, "which", lambda n: str(stub))
    write(proj, "roles/requirements.yml", "- name: a.b\n")
    out = []
    assert galaxy.sync(proj, out.append) == 0
    assert any("installing role install" in line for line in out), out


def test_a_failed_install_stops_and_reports_its_code(proj, monkeypatch, tmp_path):
    stub = tmp_path / "bin" / "ansible-galaxy"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/bin/sh\necho 'ERROR! role not found'\nexit 1\n")
    stub.chmod(0o755)
    monkeypatch.setattr(galaxy.shutil, "which", lambda n: str(stub))
    write(proj, "roles/requirements.yml", "- name: nope.nope\n")
    write(proj, "collections/requirements.yml", "collections:\n  - c.d\n")
    out = []
    rc = galaxy.sync(proj, out.append)
    assert rc == 1
    assert any("exited 1" in line for line in out)
    assert not any("collection install" in line for line in out), \
        "it carried on to the collections after the roles failed"


def test_a_project_with_nothing_says_so_rather_than_failing(proj):
    out = []
    assert galaxy.sync(proj, out.append) == 0
    assert any("nothing to install" in line for line in out)


# ---- through the API --------------------------------------------------------
def test_the_console_can_see_what_a_project_declares_and_has(client, project):
    import backend.db as db
    root = db.project_dir(project["id"])
    write(root, "roles/requirements.yml", "- name: geerlingguy.nginx\n  version: 3.1.4\n")
    (root / "roles" / "geerlingguy.nginx" / "tasks").mkdir(parents=True, exist_ok=True)

    r = client.get(f"/projects/{project['id']}/content").json()
    assert r["requirements"]["roles"]["entries"][0]["name"] == "geerlingguy.nginx"
    assert r["installed"]["roles"] == ["geerlingguy.nginx"]


def test_a_sync_becomes_a_normal_run(client, project, monkeypatch):
    """So it is tailed, listed and audited like everything else, rather than a
    progress bar nobody can find afterwards."""
    import backend.db as db
    write(db.project_dir(project["id"]), "roles/requirements.yml", "- name: a.b\n")
    monkeypatch.setattr(galaxy, "sync_commands", lambda root, force=False: [["true"]])
    monkeypatch.setattr(galaxy, "sync", lambda root, emit, **kw: (emit("done\n"), 0)[1])

    r = client.post(f"/projects/{project['id']}/content/sync", json={})
    assert r.status_code == 200, r.text
    run = db.get_run(r.json()["run_id"])
    assert run["kind"] == "galaxy" and run["project_id"] == project["id"]


def test_a_sync_without_ansible_fails_before_making_a_run_row(client, project, monkeypatch):
    """A run row for something that was never going to start is noise in the
    history."""
    def boom(root, force=False):
        raise galaxy.GalaxyError("ansible-galaxy is not installed — install Ansible first")
    monkeypatch.setattr(galaxy, "sync_commands", boom)
    r = client.post(f"/projects/{project['id']}/content/sync", json={})
    assert r.status_code == 400 and "install Ansible first" in r.json()["detail"]


def test_the_galaxy_token_is_never_returned_to_the_browser(client, project):
    import backend.db as db
    assert client.post(f"/projects/{project['id']}/content/galaxy",
                       json={"servers": ["https://hub.internal/"],
                             "token": "s3cret"}).status_code == 200
    body = client.get(f"/projects/{project['id']}").text
    assert "s3cret" not in body
    stored = db.get_project(project["id"])["galaxy_token"]
    assert stored and stored != "s3cret", "the token was stored in the clear"


def test_editing_the_server_list_does_not_drop_the_token(client, project):
    """Omitting the token means "leave it"; sending "" means "clear it". Reading
    the first as the second would silently unauthenticate the project."""
    import backend.db as db
    client.post(f"/projects/{project['id']}/content/galaxy",
                json={"servers": ["https://hub/"], "token": "s3cret"})
    before = db.get_project(project["id"])["galaxy_token"]
    client.post(f"/projects/{project['id']}/content/galaxy",
                json={"servers": ["https://hub/", "https://hub2/"]})
    assert db.get_project(project["id"])["galaxy_token"] == before
    client.post(f"/projects/{project['id']}/content/galaxy", json={"token": ""})
    assert db.get_project(project["id"])["galaxy_token"] == ""
