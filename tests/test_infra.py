"""Create Infrastructure: provider schema, project generation, and the
auto-enroll flow (Controller HTTP mocked)."""
import backend.db as db
import backend.infra as infra


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


def test_provider_schema_lists_all_providers(client):
    d = client.get("/infra/providers").json()["providers"]
    assert set(d) == {"aws", "digitalocean", "libvirt", "proxmox", "gcp", "azure"}
    for p in d.values():
        assert p["options"] and all("key" in o and "label" in o for o in p["options"])


def test_generate_all_providers_emit_normalized_output():
    for provider in infra.PROVIDERS:
        files = infra.generate(provider, {"count": 2, "name_prefix": "web", "ssh_user": "ubuntu",
                                          "environment": "prod"}, controller_key="ssh-ed25519 X ctrl")
        assert "main.tf" in files and "outputs.tf" in files and "variables.tf" in files
        assert "sysible_hosts" in files["outputs.tf"]


def test_libvirt_provider_pinned_to_compatible_major():
    """The generated libvirt HCL uses 0.7/0.8 block syntax (disk/network_interface/
    console blocks), so the provider must be pinned to 0.7.x — a bare '~> 0.7' would
    resolve to the 0.9.x plugin-framework rewrite and reject that syntax."""
    files = infra.generate("libvirt", {"count": 1, "base_image": "x", "ssh_user": "ubuntu"})
    main = files["main.tf"]
    assert 'source = "dmacvicar/libvirt"' in main
    assert 'version = "~> 0.7.0"' in main       # 0.7.z only, not 0.8/0.9
    # sanity: the block syntax the pin protects
    assert "network_interface {" in main and "disk {" in main


def test_deploy_credential_key_baked_into_cloudinit(client):
    """Picking an SSH deploy credential bakes its public key into the VMs' cloud-init
    so the same credential can log in for the Configure/Maintain steps."""
    import shutil
    if not shutil.which("ssh-keygen"):
        import pytest
        pytest.skip("ssh-keygen not available")
    import subprocess as sp
    import tempfile
    import os as _os
    # Make a throwaway keypair; store the private half as an SSH credential.
    with tempfile.TemporaryDirectory() as td:
        kp = _os.path.join(td, "k")
        sp.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", kp], check=True, capture_output=True)
        priv = open(kp).read()
        pub = open(kp + ".pub").read().strip()
    cred = client.post("/credentials", json={"name": "vm-login", "kind": "ssh", "username": "clouduser", "secret": priv}).json()

    pid = client.post("/infra", json={"name": "keyed", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "clouduser"},
                                      "deploy_credential_id": cred["id"]}).json()["project_id"]
    ci = client.get(f"/projects/{pid}/file?path=cloudinit.cfg").json()["content"]
    assert pub in ci                                   # the derived public key is present
    assert "[]" not in ci                              # not an empty authorized_keys list


def test_cloudinit_injects_controller_key():
    files = infra.generate("aws", {"ssh_user": "ubuntu", "ssh_public_key": "ssh-ed25519 DEPLOY"},
                           controller_key="ssh-ed25519 CTRLKEY")
    ci = files["cloudinit.cfg"]
    assert "ssh-ed25519 DEPLOY" in ci and "ssh-ed25519 CTRLKEY" in ci


def test_create_infra_writes_project_files(client):
    r = client.post("/infra", json={"name": "prod-web", "provider": "aws",
                                    "options": {"count": 2, "name_prefix": "web", "ssh_user": "ubuntu"}})
    assert r.status_code == 200, r.text
    pid = r.json()["project_id"]
    assert "main.tf" in r.json()["files"]
    # The generated files are on disk in the project workdir.
    files = client.get(f"/projects/{pid}/files").json()["files"]
    names = {f["path"] for f in files}
    assert "main.tf" in names and "outputs.tf" in names
    # It shows up in the infra listing.
    assert any(i["project_id"] == pid for i in client.get("/infra").json()["infra"])


def test_create_infra_unknown_provider_400(client):
    assert client.post("/infra", json={"name": "x", "provider": "oracle-cloud", "options": {}}).status_code == 400


def test_enroll_registers_hosts_in_controller(client, monkeypatch):
    import backend.controller_import as ci
    # Connect a controller (mock its probe).
    monkeypatch.setattr(ci.requests, "get", lambda url, **k: Resp(200, {"agents": []}))
    cid = client.post("/controllers", json={"name": "Prod", "base_url": "http://ctrl:9000", "api_key": "K"}).json()["controller"]["id"]

    # Create infra targeting that controller (controller-key fetch mocked to empty).
    monkeypatch.setattr(ci, "get_controller_key", lambda url, key: "ssh-ed25519 CTRL")
    pid = client.post("/infra", json={"name": "fleet", "provider": "aws",
                                     "options": {"count": 2, "name_prefix": "web", "ssh_user": "ubuntu"},
                                     "controller_id": cid}).json()["project_id"]

    # Stub terraform output + the Controller host-register call.
    import backend.app as appmod

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"web-1","ip":"10.0.0.11","user":"ubuntu"},{"name":"web-2","ip":"10.0.0.12","user":"ubuntu"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())
    registered = []
    monkeypatch.setattr(ci, "register_ssh_host",
                        lambda url, key, name, ip, user="root", environment="": (registered.append((name, ip)) or (True, "enrolled")))

    d = client.post(f"/infra/{pid}/enroll").json()
    assert d["enrolled"] == 2 and d["total"] == 2
    assert {r[0] for r in registered} == {"web-1", "web-2"}


def test_enroll_without_controller_400(client):
    pid = client.post("/infra", json={"name": "noctrl", "provider": "aws",
                                     "options": {"count": 1, "name_prefix": "web"}}).json()["project_id"]
    assert client.post(f"/infra/{pid}/enroll").status_code == 400


def test_libvirt_hypervisor_uri_local_and_remote():
    """The libvirt builder exposes the hypervisor connection URI and threads it
    into the provider block + variables — so VMs can target a local OR remote
    KVM/QEMU host (qemu+ssh://…)."""
    import backend.infra as infra
    # URI is a first-class option on the libvirt provider.
    lv = infra.provider_schema()["libvirt"]
    assert any(o["key"] == "uri" for o in lv["options"])
    # Remote hypervisor over SSH flows into the generated Terraform.
    files = infra.generate("libvirt", {
        "uri": "qemu+ssh://root@kvm-host/system", "count": 1,
        "base_image": "https://example/img.qcow2", "pool": "default"}, "")
    assert "uri = var.uri" in files["main.tf"]
    assert 'qemu+ssh://root@kvm-host/system' in files["variables.tf"]
    # Default stays the local hypervisor when unset.
    local = infra.generate("libvirt", {"count": 1, "base_image": "x"}, "")
    assert "qemu:///system" in local["variables.tf"]


def test_test_hypervisor_requires_uri(client):
    assert client.post("/infra/test-hypervisor", json={}).status_code == 400


def test_test_hypervisor_without_virsh_is_graceful(client, monkeypatch):
    # No virsh on the host → a clear, non-fatal message (not a 500). The endpoint
    # imports shutil locally, so patch the shutil module itself.
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: None)
    r = client.post("/infra/test-hypervisor", json={"uri": "qemu:///system"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "libvirt-clients" in body["output"]


def test_scaffold_configure_and_maintain(client):
    pid = client.post("/infra", json={"name": "fleet-a", "provider": "libvirt",
                                     "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    # Configure → an Ansible playbook lands in the project.
    d = client.post(f"/infra/{pid}/scaffold", json={"stage": "configure"}).json()
    assert d["path"] == "configure.yml" and d["created"] is True
    txt = client.get(f"/projects/{pid}/file?path=configure.yml").json()["content"]
    assert "hosts: all" in txt and "ansible.builtin" in txt
    # Idempotent — second call doesn't clobber.
    assert client.post(f"/infra/{pid}/scaffold", json={"stage": "configure"}).json()["created"] is False
    # Maintain → a Salt state lands.
    m = client.post(f"/infra/{pid}/scaffold", json={"stage": "maintain"}).json()
    assert m["path"] == "maintain.sls"
    assert "pkg.installed" in client.get(f"/projects/{pid}/file?path=maintain.sls").json()["content"]
    # Unknown stage → 400.
    assert client.post(f"/infra/{pid}/scaffold", json={"stage": "bogus"}).status_code == 400


def test_pipeline_runs_steps_in_sequence(client, monkeypatch):
    """A pipeline creates a run per step and executes them in order; a failing
    step cancels the rest when stop_on_failure is set."""
    import backend.app as appmod
    pid = client.post("/infra", json={"name": "seq", "provider": "libvirt",
                                     "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    inv = None   # runners are stubbed, so no real inventory is needed

    calls = []
    # Stub every runner: 1st step succeeds, 2nd fails → 3rd must be canceled.
    def fake(kind):
        def _launch(run_id):
            calls.append((run_id, kind))
            import backend.db as db
            ok = len(calls) == 1          # only the first step "succeeds"
            db.set_run_status(run_id, "success" if ok else "failed")
        return _launch
    monkeypatch.setitem(appmod.RUNNERS, "ansible", fake("ansible"))
    monkeypatch.setitem(appmod.RUNNERS, "terraform", fake("terraform"))

    steps = [
        {"kind": "terraform", "target": "apply", "tool": "tofu"},
        {"kind": "ansible", "target": "configure.yml", "inventory_id": inv},
        {"kind": "ansible", "target": "maintain-after-fail.yml", "inventory_id": inv},
    ]
    d = client.post("/pipelines/run", json={"project_id": pid, "steps": steps, "stop_on_failure": True}).json()
    assert len(d["run_ids"]) == 3
    # The worker thread runs inline-ish; poll run statuses.
    import time as _t
    for _ in range(50):
        st = [client.get(f"/runs/{r}").json()["status"] for r in d["run_ids"]]
        if st[2] in ("canceled", "success", "failed"):
            break
        _t.sleep(0.02)
    st = [client.get(f"/runs/{r}").json()["status"] for r in d["run_ids"]]
    assert st[0] == "success" and st[1] == "failed" and st[2] == "canceled"


def test_pipeline_needs_steps_and_valid_engine(client):
    pid = client.post("/infra", json={"name": "seq2", "provider": "libvirt",
                                     "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    assert client.post("/pipelines/run", json={"project_id": pid, "steps": []}).status_code == 400
    assert client.post("/pipelines/run", json={"project_id": pid,
                       "steps": [{"kind": "bogus", "target": "x"}]}).status_code == 400


def test_saved_pipeline_crud_and_run(client, monkeypatch):
    import backend.app as appmod, backend.db as db
    pid = client.post("/infra", json={"name": "svc", "provider": "libvirt",
                                     "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    # stub runners so a run "completes"
    def fake(kind):
        def _launch(run_id):
            db.set_run_status(run_id, "success")
        return _launch
    for k in ("ansible", "terraform", "salt"):
        monkeypatch.setitem(appmod.RUNNERS, k, fake(k))

    steps = [{"kind": "terraform", "target": "apply", "tool": "tofu"},
             {"kind": "ansible", "target": "configure.yml"}]
    # save
    saved = client.post("/pipelines", json={"project_id": pid, "name": "full cadence", "steps": steps}).json()["pipeline"]
    assert saved["name"] == "full cadence" and len(saved["steps"]) == 2
    # list
    assert any(p["id"] == saved["id"] and p["project_name"] == "svc" for p in client.get("/pipelines").json()["pipelines"])
    # run it → grouped runs
    d = client.post(f"/pipelines/{saved['id']}/run", json={}).json()
    assert len(d["run_ids"]) == 2 and d["group_id"]
    import time as _t
    for _ in range(50):
        grp = client.get(f"/pipelines/runs/{d['group_id']}").json()["runs"]
        if grp and all(r["status"] == "success" for r in grp):
            break
        _t.sleep(0.02)
    grp = client.get(f"/pipelines/runs/{d['group_id']}").json()["runs"]
    assert len(grp) == 2 and all(r["group_id"] == d["group_id"] for r in grp)
    # update + delete
    client.put(f"/pipelines/{saved['id']}", json={"name": "renamed"})
    assert client.get("/pipelines").json()["pipelines"][0]["name"] == "renamed"
    client.delete(f"/pipelines/{saved['id']}")
    assert not any(p["id"] == saved["id"] for p in client.get("/pipelines").json()["pipelines"])


def test_saved_pipeline_needs_name(client):
    pid = client.post("/infra", json={"name": "svc2", "provider": "libvirt",
                                     "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    assert client.post("/pipelines", json={"project_id": pid, "steps": [{"kind": "ansible", "target": "x"}]}).status_code == 400


def test_infra_to_inventory_builds_slep_inventory(client, monkeypatch):
    """After apply, the created VMs are read into a SLEP Ansible inventory
    (name→address, ansible_user, grouped by environment), reusable on re-run."""
    import backend.app as appmod
    pid = client.post("/infra", json={"name": "web", "provider": "libvirt",
                                     "options": {"count": 2, "base_image": "x", "environment": "stage web"}}).json()["project_id"]

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"web-1","ip":"10.0.0.11","user":"ubuntu"},{"name":"web-2","ip":"10.0.0.12","user":"ubuntu"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())

    d = client.post(f"/infra/{pid}/inventory", json={}).json()
    assert d["hosts"] == 2 and d["name"].endswith("(VMs)")
    iid = d["inventory_id"]
    hosts = client.get(f"/inventories/{iid}/hosts").json()["hosts"]
    byname = {h["name"]: h for h in hosts}
    assert set(byname) == {"web-1", "web-2"}
    assert byname["web-1"]["address"] == "10.0.0.11"
    assert byname["web-1"]["variables"].get("ansible_user") == "ubuntu"
    assert byname["web-1"]["groups"] == "stage_web"        # sanitized for Ansible
    # Re-run reuses the same inventory (no duplicate), refreshes hosts.
    d2 = client.post(f"/infra/{pid}/inventory", json={}).json()
    assert d2["inventory_id"] == iid
    assert len(client.get(f"/inventories/{iid}/hosts").json()["hosts"]) == 2


def test_pipeline_auto_inventory_step_backfills_following_steps(client, monkeypatch):
    """The cadence 'inventory' pseudo-step reads the applied VMs into the project's
    inventory and back-fills that inventory_id into the Ansible/Salt steps that
    follow it in the same sequence — so Create flows into Configure with no manual
    inventory hop."""
    import backend.app as appmod
    import backend.db as db
    pid = client.post("/infra", json={"name": "cadence", "provider": "libvirt",
                                     "options": {"count": 1, "base_image": "x", "environment": "prod"}}).json()["project_id"]

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"c-1","ip":"10.0.0.9","user":"ubuntu"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())

    seen = {}
    # Stub the real engines; each records the inventory_id its run carries at
    # execution time, so we can prove the inventory step back-filled it.
    def fake(kind):
        def _launch(run_id):
            seen[kind] = db.get_run(run_id).get("inventory_id")
            db.set_run_status(run_id, "success")
        return _launch
    monkeypatch.setitem(appmod.RUNNERS, "terraform", fake("terraform"))
    monkeypatch.setitem(appmod.RUNNERS, "ansible", fake("ansible"))

    steps = [
        {"kind": "terraform", "target": "apply", "tool": "tofu"},
        {"kind": "inventory", "target": "from VMs"},
        {"kind": "ansible", "target": "configure.yml", "inventory_id": None},
    ]
    d = client.post("/pipelines/run", json={"project_id": pid, "steps": steps, "stop_on_failure": True}).json()
    assert len(d["run_ids"]) == 3
    import time as _t
    for _ in range(100):
        grp = client.get(f"/pipelines/runs/{d['group_id']}").json()["runs"]
        if grp and all(r["status"] in ("success", "failed", "canceled") for r in grp):
            break
        _t.sleep(0.02)
    grp = client.get(f"/pipelines/runs/{d['group_id']}").json()["runs"]
    assert [r["status"] for r in grp] == ["success", "success", "success"]
    assert grp[1]["kind"] == "inventory"
    # The inventory step built the project's infra inventory...
    inv = db.find_inventory(pid, "infra")
    assert inv is not None
    # ...and the following Ansible step was pointed at it automatically.
    assert seen["ansible"] == inv["id"]


def test_hypervisor_key_is_managed_and_idempotent(client):
    """SLEP mints/returns a persistent managed SSH key for hypervisor connections;
    the public half + in-container keyfile path come back, and a second call
    returns the same key (no churn)."""
    import shutil
    if not shutil.which("ssh-keygen"):
        import pytest
        pytest.skip("ssh-keygen not available")
    d = client.post("/infra/hypervisor-key", json={}).json()
    assert d["public_key"].startswith("ssh-ed25519 ")
    assert d["keyfile"].endswith("/ssh/hypervisor")
    d2 = client.post("/infra/hypervisor-key", json={}).json()
    assert d2["public_key"] == d["public_key"]     # stable across calls


def test_pipeline_inventory_step_allows_empty_target(client):
    """The inventory pseudo-step needs no target (validation must not reject it)."""
    pid = client.post("/infra", json={"name": "invonly", "provider": "libvirt",
                                     "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    r = client.post("/pipelines", json={"project_id": pid, "name": "inv step",
                                        "steps": [{"kind": "inventory", "target": ""}]})
    assert r.status_code == 200
    assert r.json()["pipeline"]["steps"][0]["kind"] == "inventory"
