"""Per-project ansible.cfg — SLEP's first-class configuration file."""
import backend.projcfg as projcfg


def test_config_absent_then_seeded_from_template(client, project):
    pid = project["id"]
    # No config yet
    r = client.get(f"/projects/{pid}/config").json()
    assert r == {"exists": False, "path": "ansible.cfg", "content": ""}
    # Seed from the starter template
    seeded = client.post(f"/projects/{pid}/config/default").json()
    assert seeded["exists"] is True and seeded["path"] == "ansible.cfg"
    assert "[defaults]" in seeded["content"] and "host_key_checking" in seeded["content"]
    # It's a real file in the project dir, visible to the IDE tree
    files = client.get(f"/projects/{pid}/files").json()["files"]
    assert any(f["path"] == "ansible.cfg" for f in files)


def test_config_default_is_idempotent(client, project):
    pid = project["id"]
    client.put(f"/projects/{pid}/config", json={"content": "[defaults]\nforks = 3\n"})
    # Seeding again must not clobber an existing config
    again = client.post(f"/projects/{pid}/config/default").json()
    assert "forks = 3" in again["content"]


def test_config_write_and_read_roundtrip(client, project):
    pid = project["id"]
    body = "[defaults]\nforks = 25\nstdout_callback = yaml\n"
    saved = client.put(f"/projects/{pid}/config", json={"content": body}).json()
    assert saved["exists"] is True and "forks = 25" in saved["content"]
    assert client.get(f"/projects/{pid}/config").json()["content"] == body


def test_config_rejects_invalid_ini(client, project):
    pid = project["id"]
    r = client.put(f"/projects/{pid}/config", json={"content": "this is not ini = = ["})
    assert r.status_code == 400 and "Invalid ansible.cfg" in r.json()["detail"]


def test_config_404_for_unknown_project(client):
    assert client.get("/projects/999999/config").status_code == 404


def test_defines_helper_reads_project_cfg(client, project, tmp_path, monkeypatch):
    pid = project["id"]
    # No cfg → nothing defined
    assert projcfg.defines(pid, "defaults", "host_key_checking") is False
    client.put(f"/projects/{pid}/config",
               json={"content": "[defaults]\nhost_key_checking = True\n"})
    assert projcfg.defines(pid, "defaults", "host_key_checking") is True
    assert projcfg.defines(pid, "defaults", "forks") is False


def test_default_template_is_valid_ini():
    # The shipped starter must itself parse — a broken template would 400 on save.
    projcfg.validate(projcfg.DEFAULT_ANSIBLE_CFG)
