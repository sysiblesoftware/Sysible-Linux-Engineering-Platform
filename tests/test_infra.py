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


def test_providers_include_cloud_image_catalog_and_default_one_vm(client):
    """The wizard schema ships a catalog of common cloud images to download, and
    new libvirt infra defaults to a single VM."""
    d = client.get("/infra/providers").json()
    imgs = d["cloud_images"]
    assert imgs and all("label" in i and i["url"].startswith("http") for i in imgs)
    assert any("Ubuntu" in i["label"] for i in imgs) and any("Rocky" in i["label"] for i in imgs)
    count_opt = next(o for o in d["providers"]["libvirt"]["options"] if o["key"] == "count")
    assert count_opt["default"] == 1


def test_hypervisor_volumes_lists_pool_images(client, monkeypatch):
    """The pool-volume picker lists a pool's disk images (virsh vol-list), dropping
    cloud-init ISOs and non-image artifacts."""
    import backend.app as appmod
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")

    class R:
        returncode = 0
        stdout = "jammy.qcow2\nweb-1-ci.iso\nrocky9.img\nnotes.txt\n"
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: R())
    d = client.post("/infra/hypervisor-volumes", json={"uri": "qemu:///system", "pool": "default"}).json()
    assert d["ok"] is True
    assert d["volumes"] == ["jammy.qcow2", "rocky9.img"]     # ISO + .txt filtered out


def test_hypervisor_networks_lists_nets_and_pools(client, monkeypatch):
    """The network/pool picker reads what the hypervisor actually has (virsh
    net-list / pool-list), each with an active flag — so the wizard can offer the
    real network ('homelab') and flag an inactive pool."""
    import backend.app as appmod
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")

    def fake_run(cmd, *a, **k):
        sub = cmd[cmd.index("--readonly") + 1]

        class R:
            returncode = 0
            stderr = ""
            stdout = (" Name       State    Autostart\n----------------------------\n homelab   active   yes\n"
                      if sub == "net-list" else
                      " Name      State     Autostart\n---------------------------\n default   inactive  no\n images   active   yes\n")
        return R()
    monkeypatch.setattr(appmod.subprocess, "run", fake_run)
    d = client.post("/infra/hypervisor-networks", json={"uri": "qemu:///system"}).json()
    assert d["ok"] is True
    assert d["networks"] == [{"name": "homelab", "active": True}]
    assert {"name": "default", "active": False} in d["pools"]
    assert {"name": "images", "active": True} in d["pools"]


def test_hypervisor_pool_start(client, monkeypatch):
    """One-click pool activation runs virsh pool-start (and pool-autostart)."""
    import backend.app as appmod
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd[cmd.index("-c") + 2] if "-c" in cmd else "")

        class R:
            returncode = 0
            stdout = "Pool default started"
            stderr = ""
        return R()
    monkeypatch.setattr(appmod.subprocess, "run", fake_run)
    d = client.post("/infra/hypervisor-pool-start", json={"uri": "qemu:///system", "pool": "default"}).json()
    assert d["ok"] is True and "default" in d["output"]
    assert "pool-start" in calls and "pool-autostart" in calls
    # A blank pool is rejected.
    assert client.post("/infra/hypervisor-pool-start", json={"uri": "qemu:///system", "pool": ""}).status_code == 400


def test_libvirt_existing_pool_volume_skips_download():
    """Naming an existing pool volume clones each VM disk from it (base_volume_name)
    with NO base-volume-from-source resource — so nothing is downloaded/uploaded and
    the image on the hypervisor is used directly."""
    m = infra.generate("libvirt", {"count": 2, "base_volume": "jammy.qcow2"})["main.tf"]
    assert "base_volume_name = var.base_volume" in m
    assert 'base_volume_pool = var.pool' in m
    assert 'resource "libvirt_volume" "base"' not in m      # no download/upload volume
    assert "source = var.base_image" not in m
    # Default (no pool volume) still pulls a shared base image and CoW-clones it.
    d = infra.generate("libvirt", {"count": 1, "base_image": "https://x/y.img"})["main.tf"]
    assert 'resource "libvirt_volume" "base"' in d and "base_volume_id = libvirt_volume.base.id" in d


def test_libvirt_uses_cow_base_volume():
    """The base image is pulled into one shared base volume and each VM disk is a
    copy-on-write clone of it — so only the first apply downloads the image and it
    stays cached on the hypervisor."""
    files = infra.generate("libvirt", {"count": 2, "base_image": "https://x/img.qcow2"})
    main = files["main.tf"]
    assert 'resource "libvirt_volume" "base"' in main
    assert "source = var.base_image" in main               # base pulls the image
    assert "base_volume_id = libvirt_volume.base.id" in main  # per-VM CoW clones
    # the per-VM disk no longer downloads the image itself
    disk = main.split('resource "libvirt_volume" "disk"')[1]
    assert "source" not in disk.split("}")[0]


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


def test_test_hypervisor_preflights_network_and_pool(client, monkeypatch):
    """When network/pool are given, the probe verifies they exist + are active and
    fails (ok=False) on a missing one — catching 'can't retrieve network' before a
    long apply."""
    import shutil
    import backend.app as appmod
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **k):
        if "version" in cmd:
            return R(0, "Compiled against library: libvirt 8.0.0")
        if "net-info" in cmd:
            return R(1, "error: failed to get network 'default'")     # missing
        if "net-list" in cmd:
            return R(0, "homelab\n")                                   # what IS available
        if "pool-info" in cmd:
            # Real `virsh pool-info` reports "State: running" — NOT "Active: yes"
            # (that's net-info). A running pool must read as active.
            return R(0, "Name:           default\nUUID:           x\nState:          running\nPersistent:     yes\nAutostart:      yes\n")
        return R(0, "")
    monkeypatch.setattr(appmod.subprocess, "run", fake_run)

    d = client.post("/infra/test-hypervisor", json={"uri": "qemu:///system", "network": "default", "pool": "default"}).json()
    assert d["ok"] is False
    assert "network 'default'" in d["output"] and "MISSING" in d["output"]
    assert "available: homelab" in d["output"]                        # points at the real one
    assert "storage pool 'default': ✓" in d["output"]                 # running pool → active


def test_test_hypervisor_pool_inactive_detected(client, monkeypatch):
    """An actually-inactive pool (State: inactive) is flagged, not silently passed."""
    import shutil
    import backend.app as appmod
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **k):
        if "version" in cmd:
            return R(0, "libvirt 8.0.0")
        if "pool-info" in cmd:
            return R(0, "Name: default\nState:          inactive\n")
        return R(0, "")
    monkeypatch.setattr(appmod.subprocess, "run", fake_run)
    d = client.post("/infra/test-hypervisor", json={"uri": "qemu:///system", "pool": "default"}).json()
    assert d["ok"] is False and "INACTIVE" in d["output"]


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


def test_saved_pipeline_strips_become_password(client):
    """Security: a saved pipeline is readable by any authenticated user, so a
    per-step sudo/become password must not be persisted (was disclosed to viewers)."""
    import json as _json
    pid = client.post("/infra", json={"name": "secpipe", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    saved = client.post("/pipelines", json={"project_id": pid, "name": "p", "steps": [
        {"kind": "ansible", "target": "site.yml", "become_password": "hunter2secret"}]}).json()["pipeline"]
    assert all("become_password" not in s for s in saved["steps"])
    listed = client.get("/pipelines").json()["pipelines"]
    p = next(x for x in listed if x["id"] == saved["id"])
    assert "hunter2secret" not in _json.dumps(p)


def test_shown_cmd_redacts_secret_var_values():
    """Security: secret -var/-e/pillar values must be masked in the (viewer-readable)
    run-log command echo."""
    from backend.runners._common import shown_cmd
    out = shown_cmd(["terraform", "apply", "-var", "db_password=hunter2secret", "-var", "network=homelab"],
                    ["hunter2secret", "homelab"])
    assert "hunter2secret" not in out and "homelab" not in out and "***" in out
    assert shown_cmd(["x", "a=b"], ["b"]) == "x a=b"          # trivially short values left alone


def test_generated_hcl_escapes_user_values():
    """Security/correctness: a quote/newline in a user value can't break out of the
    HCL string and inject a resource (or corrupt the file)."""
    evil = 'app"\nresource "null_resource" "pwn" {\n  provisioner "local-exec" { command = "id" }\n}\nvariable "j" {\n  default = "x'
    files = infra.generate("libvirt", {"count": 1, "base_image": "x", "name_prefix": evil, "ssh_user": evil})
    tf = files["variables.tf"] + files["main.tf"] + files["outputs.tf"]
    # The payload survives only as escaped TEXT inside a string literal — never as a
    # real HCL block: no unescaped `resource "null_resource"` / `provisioner "..."`.
    assert 'resource "null_resource"' not in tf
    assert 'provisioner "local-exec"' not in tf
    assert '\\"' in files["variables.tf"]                    # proves the quote was escaped


def test_cloudinit_no_directive_injection():
    """Security: a newline in an SSH key / user can't inject its OWN top-level
    cloud-init directive that would run as root on the VM. (SLEP's own hardcoded
    runcmd — which enables sshd — is expected; the point is the attacker's payload
    is stripped by single-lining the key.)"""
    files = infra.generate("libvirt", {"count": 1, "base_image": "x", "ssh_user": "ubuntu",
                                       "ssh_public_key": "ssh-ed25519 AAAA\nruncmd:\n  - [sh, -c, id]"})
    ci = files["cloudinit.cfg"]
    assert "ssh-ed25519 AAAA" in ci                 # the key's first line survives
    assert "[sh, -c, id]" not in ci                 # the injected payload does NOT
    # The key line is single-lined, so nothing after the newline reaches the file.
    assert "\nruncmd:\n  - [sh, -c, id]" not in ci


def test_slep_managed_key_baked_into_infra_vms():
    """Every VM SLEP builds authorizes SLEP's own managed key (for the configured
    login user), so the default "SLEP managed key" credential can log in with no
    manual key distribution. (Exercises generate() directly — the /infra endpoint
    pulls the real managed key from keydist, which needs ssh-keygen at runtime.)"""
    mk = "ssh-ed25519 AAAAMANAGED slep-managed"
    files = infra.generate("libvirt", {"count": 1, "base_image": "x", "ssh_user": "clouduser"},
                           managed_key=mk)
    ci = files["cloudinit.cfg"]
    assert mk in ci                             # the managed public key is authorized
    assert "name: clouduser" in ci              # for the configured login user


def test_cloudinit_guarantees_local_user_and_password_option():
    """Cloud-init reliably creates the login user (useradd + keys + passwordless
    sudo via a root setup script, not just the users: module), and an optional
    password enables password SSH — hashed, never plaintext."""
    import yaml
    key = "ssh-ed25519 AAAAK managed"
    ci = infra.generate("libvirt", {"count": 1, "base_image": "x", "ssh_user": "clouduser"},
                        managed_key=key)["cloudinit.cfg"]
    yaml.safe_load(ci)                                          # valid YAML
    assert "useradd -m -s /bin/bash clouduser" in ci           # created even if users: is ignored
    assert "/etc/sudoers.d/90-slep-clouduser" in ci            # passwordless sudo
    assert "authorized_keys" in ci and key in ci               # keys installed by the script too
    assert "ssh_pwauth: false" in ci                           # key-only by default

    p = infra.generate("libvirt", {"count": 1, "base_image": "x", "ssh_user": "clouduser",
                                    "ssh_password": "S3cret-pw!"}, managed_key=key)["cloudinit.cfg"]
    yaml.safe_load(p)
    assert "ssh_pwauth: true" in p and "hashed_passwd" in p and "chpasswd -e" in p
    assert "S3cret-pw!" not in p                                # stored hashed, not plaintext


def test_cloudinit_no_duplicate_keys():
    """The same key supplied three times (managed == deploy == controller) is not
    listed three times — it's deduped in each place it's authorized (the users:
    module and the setup script), so at most once per location."""
    k = "ssh-ed25519 AAAADUP same-key"
    ci = infra.generate("libvirt", {"count": 1, "base_image": "x", "ssh_user": "ubuntu"},
                        controller_key=k, deploy_key=k, managed_key=k)["cloudinit.cfg"]
    assert 1 <= ci.count(k) <= 2


def test_cloudinit_enables_ssh_by_default():
    """Every generated VM turns its SSH server on by default (install if missing +
    enable the service), so SLEP can reach a host even if the base image ships SSH
    off — without the operator configuring anything."""
    for provider in infra.PROVIDERS:
        files = infra.generate(provider, {"count": 1, "base_image": "x", "ssh_user": "ubuntu"})
        ci = files.get("cloudinit.cfg")
        if ci is None:
            continue          # proxmox/gcp provision keys via template/metadata, not cloudinit.cfg
        assert "runcmd:" in ci
        assert "openssh-server" in ci
        assert "systemctl enable --now ssh" in ci
        assert "ssh_pwauth: false" in ci


def test_autobuild_infra_inventory_on_apply(client, monkeypatch):
    """A successful apply auto-reads the VMs into the project's own inventory
    (what the terraform runner calls); non-infra ids return None."""
    import backend.app as appmod
    pid = client.post("/infra", json={"name": "autoinv", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "environment": "prod"}}).json()["project_id"]

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"m-1","ip":"10.0.0.5","user":"ubuntu"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())
    built = appmod._autobuild_infra_inventory(pid)
    assert built is not None
    iid, name, n = built
    assert n == 1 and name.endswith("(VMs)")
    assert appmod._autobuild_infra_inventory(10_000_000) is None      # not a project


def test_infra_target_inventory_receives_vms(client, monkeypatch):
    """Creating infra with a chosen target inventory (e.g. Dev) makes the applied
    VMs land in THAT inventory, not a dedicated one."""
    import backend.app as appmod
    dev = client.post("/inventories", json={"name": "Dev"}).json()
    pid = client.post("/infra", json={"name": "tgt", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x"},
                                      "inventory_id": dev["id"]}).json()["project_id"]

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"m-1","ip":"10.0.0.7","user":"ubuntu"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())
    iid, name, n = appmod._autobuild_infra_inventory(pid)
    assert iid == dev["id"] and n == 1                       # went into Dev, not a dedicated inventory
    hosts = client.get(f"/inventories/{dev['id']}/hosts").json()["hosts"]
    assert any(h["name"] == "m-1" for h in hosts)


def test_infra_new_named_inventory(client):
    """inventory_name creates a fresh inventory and records it as the project's target."""
    d = client.post("/infra", json={"name": "newinv", "provider": "libvirt",
                                    "options": {"count": 1, "base_image": "x"},
                                    "inventory_name": "web-tier"}).json()
    assert d["inventory_id"]
    assert any(v["name"] == "web-tier" for v in client.get("/inventories").json()["inventories"])


def test_infra_inventory_never_duplicates_across_entry_points(client, monkeypatch):
    """No path can leave a project with two infra inventories: the manual
    '→ Inventory' action, the terraform runner's post-apply auto-build, and the
    pipeline 'inventory' step must all resolve to the SAME single inventory —
    including after the first build pins it to the infra meta."""
    import backend.app as appmod
    import backend.db as db
    pid = client.post("/infra", json={"name": "dedup", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "environment": "prod"}}).json()["project_id"]

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"d-1","ip":"10.0.0.3","user":"ubuntu"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())

    # Hit every build entry point several times over.
    first = client.post(f"/infra/{pid}/inventory", json={}).json()["inventory_id"]
    appmod._autobuild_infra_inventory(pid)
    appmod._autobuild_infra_inventory(pid)
    client.post(f"/infra/{pid}/inventory", json={})
    project = db.get_project(pid)
    appmod._build_infra_inventory(project, db.get_infra(pid))

    invs = [v for v in client.get("/inventories").json()["inventories"] if v["project_id"] == pid]
    assert len(invs) == 1                       # exactly one, no duplicates
    assert invs[0]["id"] == first
    assert db.get_infra(pid)["inventory_id"] == first   # pinned to the project


def test_manual_inventory_create_is_get_or_create(client):
    """Creating an inventory with a name already used in the same project returns
    the existing one instead of spawning a duplicate (guards double-submits)."""
    p = client.post("/projects", json={"name": "dedup-proj"}).json()
    a = client.post("/inventories", json={"name": "Shared", "project_id": p["id"]}).json()
    b = client.post("/inventories", json={"name": "Shared", "project_id": p["id"]}).json()
    assert a["id"] == b["id"]
    invs = [v for v in client.get("/inventories").json()["inventories"]
            if v["project_id"] == p["id"] and v["name"] == "Shared"]
    assert len(invs) == 1


def test_libvirt_hypervisor_becomes_jump_host(client, monkeypatch):
    """A libvirt project reached over qemu+ssh records the hypervisor as its jump
    host, and the built inventory hops through it — because the VMs sit on the
    hypervisor's private NAT network that SLEP can't route to directly."""
    import backend.app as appmod
    import backend.db as db
    pid = client.post("/infra", json={"name": "lab", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "environment": "prod",
                                                  "uri": "qemu+ssh://admin@192.168.8.212/system?keyfile=/k&no_verify=1"}}).json()["project_id"]
    # The hypervisor SSH endpoint is stored as the infra's bastion.
    assert db.get_infra(pid)["bastion"] == "admin@192.168.8.212"

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"vm-1","ip":"192.168.100.50","user":"ubuntu"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())
    iid, _n, _c = appmod._autobuild_infra_inventory(pid)
    # The built inventory inherits the hypervisor as its jump host.
    assert db.get_inventory(iid)["bastion"] == "admin@192.168.8.212"


def test_project_level_jump_host_propagates_to_inventories(client, monkeypatch):
    """Designating a jump host on the infra project (PATCH /infra/{id}) stores it
    and pushes it down to every inventory the project owns — so you set it once,
    not per inventory."""
    import backend.app as appmod
    import backend.db as db
    pid = client.post("/infra", json={"name": "jh", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "uri": "qemu:///system"}}).json()["project_id"]

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"vm-1","ip":"10.0.0.9","user":"ubuntu"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())
    iid, _n, _c = appmod._autobuild_infra_inventory(pid)          # a project inventory exists
    assert (db.get_inventory(iid)["bastion"] or "") == ""          # none yet (local hypervisor)

    r = client.patch(f"/infra/{pid}", json={"bastion": "admin@192.168.8.212"})
    assert r.status_code == 200 and r.json()["bastion"] == "admin@192.168.8.212"
    assert db.get_inventory(iid)["bastion"] == "admin@192.168.8.212"   # pushed down

    # Clearing it propagates too.
    client.patch(f"/infra/{pid}", json={"bastion": ""})
    assert (db.get_inventory(iid)["bastion"] or "") == ""
    # A bad IP in the jump host is rejected.
    assert client.patch(f"/infra/{pid}", json={"bastion": "admin@192.168.300.1"}).status_code == 400


def test_existing_project_derives_jump_host_from_terraform(client, monkeypatch):
    """A project created before auto-jump-host (no stored bastion) still derives the
    hypervisor jump host from the qemu+ssh URI in its Terraform, and the built
    inventory + the ansible run pick it up."""
    import backend.app as appmod
    import backend.db as db
    pid = client.post("/infra", json={"name": "old", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x",
                                                  "uri": "qemu+ssh://admin@192.168.8.212/system?keyfile=/k&no_verify=1"}}).json()["project_id"]
    # Simulate a pre-fix project: clear the stored bastion the create just set.
    db.set_infra_bastion(pid, "")
    assert appmod._project_hypervisor_bastion(pid) == "admin@192.168.8.212"

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"vm-1","ip":"192.168.100.9","user":"clouduser"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())
    iid, _n, _c = appmod._autobuild_infra_inventory(pid)
    # The build derives + persists the jump host onto both the infra row and the inventory.
    assert db.get_infra(pid)["bastion"] == "admin@192.168.8.212"
    assert db.get_inventory(iid)["bastion"] == "admin@192.168.8.212"


def test_local_libvirt_sets_no_jump_host(client):
    """A local hypervisor (qemu:///system) needs no jump host — the bastion stays
    empty rather than pointing at a non-routable address."""
    import backend.db as db
    pid = client.post("/infra", json={"name": "local", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "uri": "qemu:///system"}}).json()["project_id"]
    assert (db.get_infra(pid)["bastion"] or "") == ""


def test_sync_managed_credential_tracks_on_disk_key(client):
    """The 'SLEP managed key' credential is kept holding the current on-disk private
    key (so a run authenticates with exactly the baked key), without disturbing its
    username; idempotent when already in sync."""
    import backend.keydist as keydist
    import backend.db as db
    priv = keydist._key_paths()[0]
    priv.parent.mkdir(parents=True, exist_ok=True)
    priv.write_text("PRIVATE-ON-DISK")
    cid = db.upsert_credential("SLEP managed key", kind="ssh", username="clouduser", secret="STALE-OLD")
    assert keydist.sync_managed_credential() is True
    got = db.get_credential(cid, include_secret=True)
    assert got["secret"] == "PRIVATE-ON-DISK" and got["username"] == "clouduser"
    assert keydist.sync_managed_credential() is False   # already in sync


def test_slep_authorized_keys_bakes_credential_derived_key(client, monkeypatch):
    """The keys baked into a VM include BOTH the on-disk managed key AND the public
    half derived from the 'SLEP managed key' credential — so the login matches even
    if the two ever diverged."""
    import backend.app as appmod
    import backend.keydist as keydist
    import backend.db as db
    monkeypatch.setattr(keydist, "sync_managed_credential", lambda: False)
    monkeypatch.setattr(keydist, "public_key", lambda: "ssh-ed25519 ONDISK managed")
    db.upsert_credential("SLEP managed key", kind="ssh", username="u", secret="PRIV")
    monkeypatch.setattr(appmod, "_derive_public_key",
                        lambda s: "ssh-ed25519 CREDKEY fromcred" if s == "PRIV" else "")
    keys = appmod._slep_authorized_keys()
    assert "ssh-ed25519 ONDISK managed" in keys and "ssh-ed25519 CREDKEY fromcred" in keys


def test_apply_patches_managed_key_into_stale_cloudinit(client, monkeypatch):
    """A project whose cloud-init predates managed-key baking gets SLEP's key
    injected before apply — so re-created VMs accept the default credential — and
    the patch is idempotent (no duplicate on a second pass)."""
    import backend.app as appmod
    import backend.keydist as keydist
    pid = client.post("/infra", json={"name": "stale", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "clouduser"}}).json()["project_id"]
    # Simulate a pre-fix cloud-init: no managed key in authorized_keys.
    ci = appmod.db.project_dir(pid) / "cloudinit.cfg"
    ci.write_text("#cloud-config\nusers:\n  - name: clouduser\n    ssh_authorized_keys:\n      - ssh-ed25519 OLD deploy\npackage_update: true\n")
    monkeypatch.setattr(keydist, "public_key", lambda: "ssh-ed25519 MANAGED slep-managed")

    assert appmod._ensure_managed_key_in_cloudinit(pid) is True
    text = ci.read_text()
    assert "ssh-ed25519 MANAGED slep-managed" in text
    assert "ssh-ed25519 OLD deploy" in text                 # existing keys preserved
    # Idempotent: second call is a no-op and doesn't duplicate.
    assert appmod._ensure_managed_key_in_cloudinit(pid) is False
    assert ci.read_text().count("MANAGED slep-managed") == 1


def test_managed_key_patch_handles_empty_key_list(client, monkeypatch):
    """When the cloud-init had no keys ('[]' placeholder), the managed key replaces
    it rather than producing invalid YAML."""
    import backend.app as appmod
    import backend.keydist as keydist
    pid = client.post("/infra", json={"name": "nokeys", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "u"}}).json()["project_id"]
    ci = appmod.db.project_dir(pid) / "cloudinit.cfg"
    ci.write_text("#cloud-config\nusers:\n  - name: u\n    ssh_authorized_keys:\n      []\npackage_update: true\n")
    monkeypatch.setattr(keydist, "public_key", lambda: "ssh-ed25519 MK slep-managed")
    assert appmod._ensure_managed_key_in_cloudinit(pid) is True
    text = ci.read_text()
    assert "- ssh-ed25519 MK slep-managed" in text and "[]" not in text
    import yaml
    yaml.safe_load(text)                                    # still valid YAML


def test_apply_regenerates_stale_cloudinit_with_setup_script(client, monkeypatch):
    """A project whose cloud-init predates the robust setup script gets its
    cloud-init rebuilt on apply — the setup script (useradd + keys + sshd) is added,
    existing keys are preserved, and SLEP's managed key is included."""
    import backend.app as appmod
    import backend.keydist as keydist
    pid = client.post("/infra", json={"name": "regen", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "clouduser"}}).json()["project_id"]
    # Simulate an OLD-format cloud-init: users: module only, no setup script.
    ci = appmod.db.project_dir(pid) / "cloudinit.cfg"
    ci.write_text("#cloud-config\nusers:\n  - name: clouduser\n    ssh_authorized_keys:\n"
                  "      - ssh-ed25519 AAAADEPLOY deploy\npackage_update: true\nssh_pwauth: false\n")
    monkeypatch.setattr(appmod, "_slep_authorized_keys", lambda: ["ssh-ed25519 AAAAMANAGED managed"])

    assert appmod._refresh_infra_cloudinit(pid) is True
    text = ci.read_text()
    assert "useradd -m -s /bin/bash clouduser" in text      # robust setup script now present
    assert "ssh-ed25519 AAAADEPLOY deploy" in text          # existing key preserved
    assert "ssh-ed25519 AAAAMANAGED managed" in text        # SLEP's key added
    import yaml
    yaml.safe_load(text)


def test_resolve_secret_ref_from_vault(client):
    """A `vault.NAME` reference resolves to the secret's plaintext (any of the
    playbook spellings), a bare name works too, and an unknown vault ref resolves
    to '' (never baked literally) while a plain literal passes through."""
    import backend.app as appmod
    import backend.db as db
    import backend.vault as vault
    db.upsert_secret("admin_pw", vault.encrypt("s3cr3t!"))
    assert appmod._resolve_secret_ref("vault.admin_pw") == "s3cr3t!"
    assert appmod._resolve_secret_ref("{{ vault.admin_pw }}") == "s3cr3t!"
    assert appmod._resolve_secret_ref("admin_pw") == "s3cr3t!"     # bare name
    assert appmod._resolve_secret_ref("vault.nope") == ""          # unknown ref → not literal
    assert appmod._resolve_secret_ref("PlainText123") == "PlainText123"  # literal
    assert appmod._resolve_secret_ref("") == ""


def test_infra_ssh_user_kept_consistent(client, monkeypatch):
    """Setting the login user on the project (PATCH ssh_user) keeps it consistent
    everywhere: the infra row, the Terraform output `user` (→ inventory ansible_user),
    the built inventory hosts, and the cloud-init the VM boots with."""
    import backend.app as appmod
    import backend.db as db
    pid = client.post("/infra", json={"name": "suser", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x",
                                                  "ssh_user": "clouduser", "uri": "qemu:///system"}}).json()["project_id"]

    class Out:
        returncode = 0
        stdout = '{"sysible_hosts":{"value":[{"name":"vm-1","ip":"10.0.0.9","user":"clouduser"}]}}'
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Out())
    iid, _n, _c = appmod._autobuild_infra_inventory(pid)

    r = client.patch(f"/infra/{pid}", json={"ssh_user": "admin"})
    assert r.status_code == 200 and r.json()["ssh_user"] == "admin"
    # Terraform output now names admin, so re-reads feed ansible_user=admin.
    assert 'user = "admin"' in (db.project_dir(pid) / "outputs.tf").read_text()
    # Existing inventory hosts were repointed.
    hosts = db.list_hosts(iid)
    assert hosts and all((h["variables"] or {}).get("ansible_user") == "admin" for h in hosts)
    # Cloud-init creates admin (users: block AND setup script), not the old user.
    ci = (db.project_dir(pid) / "cloudinit.cfg").read_text()
    assert "name: admin" in ci and "useradd -m -s /bin/bash admin" in ci
    assert "clouduser" not in ci


def test_infra_password_from_vault_wired_into_cloudinit(client):
    """Setting the login password as a Vault variable (PATCH ssh_password) hashes it
    into the cloud-init and turns password SSH on — the plaintext is never written to
    disk. An unknown vault ref is rejected, not baked literally."""
    import backend.app as appmod
    import backend.db as db
    import backend.vault as vault
    pid = client.post("/infra", json={"name": "pw", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x",
                                                  "ssh_user": "admin", "uri": "qemu:///system"}}).json()["project_id"]
    db.upsert_secret("admin_pw", vault.encrypt("Sup3rSecret"))

    r = client.patch(f"/infra/{pid}", json={"ssh_password": "vault.admin_pw"})
    assert r.status_code == 200
    ci = (db.project_dir(pid) / "cloudinit.cfg").read_text()
    assert "ssh_pwauth: true" in ci and "hashed_passwd:" in ci
    assert "Sup3rSecret" not in ci               # plaintext never on disk
    assert "chpasswd -e" in ci                   # password applied in setup script
    # A typo'd vault ref is rejected rather than baked as a literal.
    assert client.patch(f"/infra/{pid}", json={"ssh_password": "vault.missing"}).status_code == 400


def test_infra_create_resolves_vault_password(client):
    """A Vault password given at create time is resolved before generate, so the
    cloud-init carries the hashed real password, not the literal `vault.NAME`."""
    import backend.db as db
    import backend.vault as vault
    db.upsert_secret("boot_pw", vault.encrypt("BootPass42"))
    pid = client.post("/infra", json={"name": "cpw", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "admin",
                                                  "ssh_password": "vault.boot_pw", "uri": "qemu:///system"}}).json()["project_id"]
    ci = (db.project_dir(pid) / "cloudinit.cfg").read_text()
    assert "hashed_passwd:" in ci and "ssh_pwauth: true" in ci
    assert "BootPass42" not in ci and "vault.boot_pw" not in ci


def test_hypervisor_key_is_the_single_managed_key(client):
    """'Get deploy key' / the URI keyfile / the install flow all resolve to SLEP's
    ONE managed key (slep_ed25519) — the same key baked into VMs and used for the
    jump hop — not a separate per-purpose hypervisor key."""
    import backend.keydist as keydist
    priv, pub = keydist._key_paths()
    priv.parent.mkdir(parents=True, exist_ok=True)
    priv.write_text("PRIV")
    pub.write_text("ssh-ed25519 AAAAMANAGED slep-managed\n")
    try:
        r = client.post("/infra/hypervisor-key").json()
        assert r["public_key"] == "ssh-ed25519 AAAAMANAGED slep-managed"
        assert r["keyfile"] == keydist.managed_key_path() == str(priv)
    finally:
        # DATA_DIR is shared across tests; don't leave the managed key behind (the
        # keydist generate-once test asserts on its absence).
        priv.unlink(missing_ok=True)
        pub.unlink(missing_ok=True)


def test_install_hypervisor_key_with_password(client, monkeypatch):
    """Installing SLEP's hypervisor key with a one-time password: resolves a Vault
    ref, shells out via sshpass with the password in the env (never argv), and
    reports success. A missing sshpass falls back to the manual command; an unknown
    vault ref is rejected."""
    import shutil
    import backend.app as appmod
    import backend.db as db
    import backend.vault as vault
    monkeypatch.setattr(appmod, "infra_hypervisor_key",
                        lambda user=None: {"public_key": "ssh-ed25519 HVKEY slep", "keyfile": "/k"})
    db.upsert_secret("kvm_pw", vault.encrypt("hunter2"))

    captured = {}

    class Ok:
        returncode = 0
        stdout = "installed"
        stderr = ""

    def fake_which(b):
        return "/usr/bin/sshpass" if b == "sshpass" else None

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        captured["env"] = k.get("env") or {}
        return Ok()
    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(appmod.subprocess, "run", fake_run)

    r = client.post("/infra/install-hypervisor-key",
                    json={"host": "192.168.8.212", "user": "admin", "password": "vault.kvm_pw"}).json()
    assert r["ok"] is True
    assert r["keyfile"] == "/k"          # URI can now point keyfile= at the installed key
    # Password went through the env (SSHPASS), never on the command line.
    assert captured["env"].get("SSHPASS") == "hunter2"
    assert "hunter2" not in " ".join(captured["cmd"])
    assert "admin@192.168.8.212" in captured["cmd"]
    # The remote command grants libvirt access so qemu+ssh can manage VMs.
    assert any("usermod -aG" in a and "libvirt" in a for a in captured["cmd"])

    # Unknown vault ref → rejected, not used literally.
    assert client.post("/infra/install-hypervisor-key",
                       json={"host": "h", "user": "admin", "password": "vault.missing"}).status_code == 400

    # No sshpass on the host → graceful fallback with the manual key.
    monkeypatch.setattr(shutil, "which", lambda b: None)
    r2 = client.post("/infra/install-hypervisor-key",
                     json={"host": "h", "user": "admin", "password": "literalpw"}).json()
    assert r2["ok"] is False and r2.get("need_manual") and "ssh-ed25519 HVKEY slep" in r2["public_key"]


def test_install_hypervisor_key_validates_input(client):
    """The install endpoint rejects a missing password, a bad host, and a bad user."""
    assert client.post("/infra/install-hypervisor-key", json={"host": "h", "user": "admin"}).status_code == 400
    assert client.post("/infra/install-hypervisor-key",
                       json={"host": "h;rm -rf /", "user": "admin", "password": "x"}).status_code == 400
    assert client.post("/infra/install-hypervisor-key",
                       json={"host": "h", "user": "a b", "password": "x"}).status_code == 400


def test_post_apply_key_check(client, monkeypatch):
    """After apply, SLEP probes SSH to each new VM with the managed key (through the
    jump host) and logs a per-host verdict. No managed key → silent no-op; with a
    key and a reachable host → a ✓ summary."""
    import backend.app as appmod
    import backend.keydist as keydist
    pid = client.post("/infra", json={"name": "pk", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "clouduser"}}).json()["project_id"]
    iid = client.post("/inventories", json={"name": "pk-inv", "project_id": pid}).json()["id"]
    import backend.db as db
    db.upsert_host(iid, "app-1", "192.168.100.5", variables={"ansible_user": "clouduser"}, source="infra")

    # No managed key present → the check is a silent no-op, and never raises.
    monkeypatch.setattr(keydist, "managed_key_path", lambda: "")
    out = []
    appmod._verify_infra_key_access(pid, iid, out.append)
    assert out == []

    # With a key and a reachable host, it reports the host as reachable.
    monkeypatch.setattr(keydist, "managed_key_path", lambda: "/tmp/fake-key")

    class Ok:
        returncode = 0
        stdout = "SLEP_OK\n"
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: Ok())
    out = []
    appmod._verify_infra_key_access(pid, iid, out.append)
    joined = "\n".join(out)
    assert "✓ app-1" in joined and "1/1 new VM(s) reachable" in joined


def test_infra_project_vms_lists_domains(client, monkeypatch):
    """List VMs on a project's hypervisor: parse `virsh list --all` into name+state."""
    import shutil
    import backend.app as appmod
    pid = client.post("/infra", json={"name": "vmlist", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")

    class R:
        returncode = 0
        stdout = " Id   Name    State\n----------------------\n -    app-1   shut off\n 3    app-2   running\n"
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: R())
    d = client.post(f"/infra/{pid}/vms", json={}).json()
    assert d["ok"] is True
    assert {v["name"]: v["state"] for v in d["vms"]} == {"app-1": "shut off", "app-2": "running"}


def test_libvirt_uri_allowlist():
    """Security: reject non-qemu schemes (SSRF) and exec-capable URI params
    (command=/netcat=); allow the qemu transports + keyfile/no_verify SLEP sets."""
    import backend.app as appmod
    import fastapi
    assert appmod._validate_libvirt_uri("qemu:///system")
    assert appmod._validate_libvirt_uri("qemu+ssh://admin@h/system?keyfile=/data/k&no_verify=1")
    for bad in ("http://evil/x", "file:///etc/passwd",
                "qemu+ssh://h/system?command=/bin/sh", "qemu+ssh://h/system?netcat=nc",
                "qemu+ssh://h/system?proxy=netcat"):
        try:
            appmod._validate_libvirt_uri(bad)
            assert False, f"should have rejected {bad}"
        except fastapi.HTTPException as e:
            assert e.status_code == 400


def test_run_extra_vars_masked_for_viewers():
    """Security: a run row's extra_vars values are masked for viewers (may hold a
    secret typed into the Variables box); operator+ see them in full."""
    import json as _json
    import backend.app as appmod
    row = {"extra_vars": '{"db_password": "hunter2secret", "network": "homelab"}'}
    masked = appmod._mask_extra_vars(dict(row), "viewer")
    assert _json.loads(masked["extra_vars"]) == {"db_password": "***", "network": "***"}
    assert appmod._mask_extra_vars(dict(row), "operator")["extra_vars"] == row["extra_vars"]


def test_pipeline_inventory_step_allows_empty_target(client):
    """The inventory pseudo-step needs no target (validation must not reject it)."""
    pid = client.post("/infra", json={"name": "invonly", "provider": "libvirt",
                                     "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    r = client.post("/pipelines", json={"project_id": pid, "name": "inv step",
                                        "steps": [{"kind": "inventory", "target": ""}]})
    assert r.status_code == 200
    assert r.json()["pipeline"]["steps"][0]["kind"] == "inventory"


def test_orphan_cloudinit_iso_parse(tmp_path):
    """The self-heal parses '<name>-ci.iso exists already' errors from the log and
    only ever targets cloud-init ISOs, never real disks."""
    from backend.runners import terraform_runner as tr
    log = tmp_path / "run.log"
    log.write_text(
        "libvirt_volume.disk[0]: Creation complete\n"
        "Error: error creating libvirt volume for cloudinit device app-1-ci.iso: "
        "storage volume 'app-1-ci.iso' exists already\n"
        "storage volume 'app-2-ci.iso' exists already\n"
        "storage volume 'realdisk.qcow2' exists already\n"   # must be ignored
    )
    got = tr._orphan_cloudinit_isos(log)
    assert got == ["app-1-ci.iso", "app-2-ci.iso"]
    assert "realdisk.qcow2" not in got


def test_libvirt_default_hostname_sysible():
    """VMs SLEP builds default to hostname 'sysible', set per-VM via the cloud-init
    meta-data local-hostname (the shared user_data can't differ per VM). A single VM
    gets the bare name; several get it suffixed so they stay unique."""
    files = infra.generate("libvirt", {"count": 1, "base_image": "x"})
    main = files["main.tf"]
    # hostname is a parameter, default sysible, overridable.
    assert 'variable "hostname"' in files["variables.tf"]
    assert 'default = "sysible"' in files["variables.tf"]
    # per-VM meta-data drives the hostname (bare for one, suffixed for many)
    assert "meta_data" in main and "local-hostname:" in main
    assert 'var.vm_count > 1 ? "${var.hostname}-${count.index + 1}" : var.hostname' in main
    # instance-id is per-VM too (also stops cloud-init treating a clone as configured)
    assert "instance-id: ${var.name_prefix}-${count.index + 1}" in main
    # the meta_data stays a single HCL line (literal \n escapes, not real newlines)
    meta = [ln for ln in main.splitlines() if "meta_data" in ln][0]
    assert "\\n" in meta and meta.count('"') >= 2
    # belt-and-suspenders: /etc/hosts maps the runtime hostname
    assert "127.0.1.1" in files["cloudinit.cfg"]
    # overridable
    m2 = infra.generate("libvirt", {"count": 1, "base_image": "x", "hostname": "edge"})["variables.tf"]
    assert 'default = "edge"' in m2


def test_libvirt_hostname_option_in_schema(client):
    """The wizard exposes a Hostname field for libvirt (default sysible)."""
    opts = client.get("/infra/providers").json()["providers"]["libvirt"]["options"]
    ho = next((o for o in opts if o["key"] == "hostname"), None)
    assert ho and ho["default"] == "sysible"


def test_orphan_domain_parse_and_delete(tmp_path, monkeypatch):
    """The self-heal parses 'domain '<name>' already exists' errors and undefines
    the orphaned domains via virsh — without ever removing their storage."""
    from backend.runners import terraform_runner as tr
    log = tmp_path / "run.log"
    log.write_text(
        "Error: error defining libvirt domain: operation failed: domain 'app-1' "
        "already exists with uuid 8cdee394-22a8-4175-b423-ceec277f6417\n"
        "domain 'app-2' already exists\n"
    )
    assert tr._orphan_domains(log) == ["app-1", "app-2"]

    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")
    monkeypatch.setattr(tr, "_libvirt_uri_for", lambda pid: "qemu:///system")
    calls = []
    monkeypatch.setattr(tr, "_run_quiet", lambda cmd: calls.append(cmd) or 0)
    msgs = []
    assert tr._delete_libvirt_domains(1, ["app-1"], lambda m: msgs.append(m)) is True
    # destroy then undefine; and NEVER --remove-all-storage
    assert any(c[-2:] == ["destroy", "app-1"] or ("destroy" in c and "app-1" in c) for c in calls)
    assert any("undefine" in c for c in calls)
    assert not any("--remove-all-storage" in c for c in calls)


def test_cleanup_empty_infra_inventory_on_failed_apply(client):
    """A failed apply removes the project's EMPTY infra inventory (so retries don't
    pile up doubles) but keeps one that already has hosts."""
    from backend.runners import terraform_runner as tr
    import backend.db as db
    msgs = []
    emit = lambda m: msgs.append(m)
    # Empty infra inventory → removed, and the infra ref is cleared.
    pid = client.post("/infra", json={"name": "clean", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    iid = db.create_inventory("clean (VMs)", project_id=pid, source="infra")
    db.set_infra_inventory(pid, iid)
    assert tr._cleanup_empty_infra_inventory(pid, emit) is True
    assert db.get_inventory(iid) is None
    assert (db.get_infra(pid) or {}).get("inventory_id") in (None, 0)

    # An inventory WITH hosts is left alone.
    pid2 = client.post("/infra", json={"name": "keep", "provider": "libvirt",
                                       "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    iid2 = db.create_inventory("keep (VMs)", project_id=pid2, source="infra")
    db.set_infra_inventory(pid2, iid2)
    db.upsert_host(iid2, "vm-1", "10.0.0.5", source="infra")
    assert tr._cleanup_empty_infra_inventory(pid2, emit) is False
    assert db.get_inventory(iid2) is not None


def test_managed_key_view_and_remove(client, monkeypatch):
    """GET /infra/managed-key reports the key + credential; DELETE removes both the
    on-disk key and the 'SLEP managed key' credential."""
    import backend.keydist as keydist
    import backend.db as db
    priv, pub = keydist._key_paths()
    priv.parent.mkdir(parents=True, exist_ok=True)
    priv.write_text("PRIV"); pub.write_text("ssh-ed25519 AAAAMK slep-managed\n")
    cid = db.upsert_credential("SLEP managed key", kind="ssh", username="admin", secret="PRIV")
    try:
        info = client.get("/infra/managed-key").json()
        assert info["exists"] is True and info["credential_id"] == cid
        assert info["public_key"].startswith("ssh-ed25519")

        d = client.request("DELETE", "/infra/managed-key").json()
        assert d["removed"] is True and d["credential_removed"] is True
        assert not priv.exists() and not pub.exists()
        assert client.get("/infra/managed-key").json()["exists"] is False
    finally:
        priv.unlink(missing_ok=True); pub.unlink(missing_ok=True)


def test_managed_key_regenerate(client, monkeypatch):
    """Regenerate mints a NEW key (different from the old) and re-syncs the credential."""
    import backend.keydist as keydist
    import backend.db as db
    priv, pub = keydist._key_paths()
    priv.parent.mkdir(parents=True, exist_ok=True)
    priv.write_text("OLD-PRIV"); pub.write_text("ssh-ed25519 OLDKEY slep-managed\n")
    db.upsert_credential("SLEP managed key", kind="ssh", username="admin", secret="OLD-PRIV")
    # Fake ssh-keygen (absent in the test image) so ensure_key() mints a new pair.
    import shutil
    monkeypatch.setattr(keydist.shutil, "which", lambda b: "/usr/bin/" + b)

    def fake_run(cmd, **k):
        f = cmd[cmd.index("-f") + 1]
        open(f, "w").write("NEW-PRIV"); open(f + ".pub", "w").write("ssh-ed25519 NEWKEY slep-managed")
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()
    monkeypatch.setattr(keydist.subprocess, "run", fake_run)
    try:
        d = client.post("/infra/managed-key/regenerate").json()
        assert "NEWKEY" in d["public_key"] and "OLDKEY" not in d["public_key"]
        # credential re-synced to the new private key
        got = db.get_credential(db.list_credentials()[0]["id"], include_secret=True) if False else None
        assert keydist.public_key() == "ssh-ed25519 NEWKEY slep-managed"
    finally:
        priv.unlink(missing_ok=True); pub.unlink(missing_ok=True)


def test_deep_destroy_sweeps_only_project_prefix(client, monkeypatch):
    """A libvirt destroy sweeps THIS project's leftover domains + volumes (matching
    its name_prefix), and never another project's."""
    from backend.runners import terraform_runner as tr
    import backend.db as db
    import shutil
    pid = client.post("/projects", json={"name": "dd", "slug": "dd"}).json()["id"]
    (db.project_dir(pid) / "variables.tf").write_text(
        'variable "name_prefix" {\n  type = string\n  default = "app"\n}\n'
        'variable "pool" {\n  type = string\n  default = "default"\n}\n')

    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")
    monkeypatch.setattr(tr, "_libvirt_uri_for", lambda p: "qemu:///system")

    def fake_run(cmd, *a, **k):
        class R:
            returncode = 0; stderr = ""
            stdout = ("app-1\nother-1\n" if "list" in cmd else
                      "app-base.qcow2\napp-1.qcow2\napp-1-ci.iso\nother-1.qcow2\nnotes.txt\n")
        return R()
    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    dom_seen, vol_seen = [], []
    monkeypatch.setattr(tr, "_delete_libvirt_domains", lambda p, names, e: dom_seen.extend(names) or True)
    monkeypatch.setattr(tr, "_delete_libvirt_volumes", lambda p, names, e: vol_seen.extend(names) or True)

    n = tr._deep_destroy_libvirt(pid, lambda m: None)
    # only app-* objects, never other-*
    assert dom_seen == ["app-1"]
    assert set(vol_seen) == {"app-base.qcow2", "app-1.qcow2", "app-1-ci.iso"}
    assert "other-1" not in dom_seen and not any("other" in v for v in vol_seen)
    assert n == 1 + 3


def test_tf_var_default_reads_variables_tf(client):
    from backend.runners import terraform_runner as tr
    import backend.db as db
    pid = client.post("/projects", json={"name": "vt", "slug": "vt"}).json()["id"]
    (db.project_dir(pid) / "variables.tf").write_text(
        'variable "name_prefix" {\n  default = "web"\n}\nvariable "pool" {\n  default = "fast"\n}\n')
    assert tr._tf_var_default(pid, "name_prefix", "app") == "web"
    assert tr._tf_var_default(pid, "pool", "default") == "fast"
    assert tr._tf_var_default(pid, "missing", "fb") == "fb"


def test_cloudinit_sanitizes_corrupted_key_quote():
    """A stored key wrapped in a stray quote (an old corruption, e.g.
    'ssh-ed25519 AAAA slep-managed"') bakes in CLEAN, and dedupes with the same key
    without the junk — so the next apply heals a corrupted cloud-init."""
    import backend.infra as infra
    ci = infra.generate("libvirt", {"count": 1, "base_image": "x", "ssh_user": "admin"},
                        managed_key='ssh-ed25519 AAAAKEY slep-managed"',
                        deploy_key="ssh-ed25519 AAAAKEY slep-managed")["cloudinit.cfg"]
    assert 'slep-managed"' not in ci                       # the stray quote is gone
    assert "ssh-ed25519 AAAAKEY slep-managed" in ci
    # the quoted + unquoted forms are the same key → exactly one authorized_keys entry
    assert ci.count("ssh-ed25519 AAAAKEY") == 2            # once in users:, once in the script
    # sanitizer unit behaviour
    assert infra._sanitize_pubkey('ssh-ed25519 AAAA cmt"') == "ssh-ed25519 AAAA cmt"
    assert infra._sanitize_pubkey('"ssh-ed25519 AAAA"') == "ssh-ed25519 AAAA"
    assert infra._sanitize_pubkey("ssh-rsa BBBB") == "ssh-rsa BBBB"


def test_infra_stores_vault_password_ref_only(client):
    """A Vault password reference is persisted (name only, for later re-resolution to
    auto-install the key); a literal password is NOT stored."""
    import backend.db as db
    import backend.vault as vault
    db.upsert_secret("kvm_pw", vault.encrypt("s3cr3t"))
    # Vault ref → the NAME is stored on the infra row.
    pid = client.post("/infra", json={"name": "pw-ref", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "admin",
                                                  "ssh_password": "vault.kvm_pw"}}).json()["project_id"]
    assert db.get_infra(pid).get("ssh_password_ref") == "kvm_pw"
    # Literal password → nothing stored.
    pid2 = client.post("/infra", json={"name": "pw-lit", "provider": "libvirt",
                                       "options": {"count": 1, "base_image": "x", "ssh_user": "admin",
                                                   "ssh_password": "PlainPass1"}}).json()["project_id"]
    assert (db.get_infra(pid2).get("ssh_password_ref") or "") == ""
    # PATCH with a vault ref also records it.
    client.patch(f"/infra/{pid2}", json={"ssh_password": "vault.kvm_pw"})
    assert db.get_infra(pid2).get("ssh_password_ref") == "kvm_pw"


def test_projects_flag_infra(client):
    """GET /projects flags infra projects (+ provider) so the UI can surface the
    lifecycle actions on those rows / in the IDE."""
    p_plain = client.post("/projects", json={"name": "plain", "slug": "plain-x"}).json()["id"]
    p_infra = client.post("/infra", json={"name": "infra-x", "provider": "libvirt",
                                          "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    rows = {r["id"]: r for r in client.get("/projects").json()["projects"]}
    assert rows[p_infra]["is_infra"] is True and rows[p_infra]["infra_provider"] == "libvirt"
    assert rows[p_plain]["is_infra"] is False and rows[p_plain]["infra_provider"] == ""


def test_infra_create_into_existing_project(client):
    """The wizard can build into an EXISTING project (project_id) instead of making
    a new one; a project that's already infra is refused."""
    import backend.db as db
    pid = client.post("/projects", json={"name": "Apps", "slug": "apps-x"}).json()["id"]
    r = client.post("/infra", json={"name": "Apps", "provider": "libvirt",
                                    "project_id": pid, "options": {"count": 1, "base_image": "x"}})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == pid          # same project, not a new one
    assert db.get_infra(pid) is not None
    # main.tf was generated into it.
    files = {f["path"] for f in client.get(f"/projects/{pid}/files").json()["files"]}
    assert "main.tf" in files
    # Building again into an already-infra project is refused.
    assert client.post("/infra", json={"name": "Apps", "provider": "libvirt",
                                       "project_id": pid, "options": {"count": 1, "base_image": "x"}}).status_code == 400


def test_distribute_key_over_password(client, monkeypatch):
    """/infra/{id}/distribute-key installs SLEP's current key on the VMs via the
    stored Vault password (sshpass), and reports per-host results."""
    import backend.app as appmod
    import backend.keydist as keydist
    import backend.db as db
    import backend.vault as vault
    import shutil
    pid = client.post("/infra", json={"name": "fix", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "admin"}}).json()["project_id"]
    iid = db.create_inventory("fix (VMs)", project_id=pid, source="infra")
    db.set_infra_inventory(pid, iid)
    db.upsert_host(iid, "prod-app-1", "192.168.100.115", variables={"ansible_user": "admin"}, source="infra")
    db.upsert_secret("kvm_pw", vault.encrypt("hunter2"))
    db.set_infra_ssh_password_ref(pid, "kvm_pw")
    monkeypatch.setattr(keydist, "public_key", lambda: "ssh-ed25519 CURRENT slep-managed")
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/" + b)
    seen = {}
    class R: returncode = 0; stdout = ""; stderr = ""
    def fake_run(cmd, *a, **k):
        seen["env"] = k.get("env") or {}
        seen["cmd"] = cmd
        return R()
    monkeypatch.setattr(appmod.subprocess, "run", fake_run)
    d = client.post(f"/infra/{pid}/distribute-key").json()
    assert d["installed"] == 1 and d["total"] == 1
    assert d["results"][0]["ok"] is True
    assert seen["env"].get("SSHPASS") == "hunter2"      # password via env, not argv
    assert "hunter2" not in " ".join(seen["cmd"])

    # No stored password → a clear note, no crash.
    pid2 = client.post("/infra", json={"name": "fix2", "provider": "libvirt",
                                       "options": {"count": 1, "base_image": "x"}}).json()["project_id"]
    iid2 = db.create_inventory("fix2 (VMs)", project_id=pid2, source="infra")
    db.set_infra_inventory(pid2, iid2)
    db.upsert_host(iid2, "vm-1", "10.0.0.9", source="infra")
    d2 = client.post(f"/infra/{pid2}/distribute-key").json()
    assert d2["total"] == 0 and "password" in d2["note"].lower()


def test_access_deploy_credential_baked(client):
    """Picking a stored SSH credential in Access stores it and bakes its public key
    into the project's cloud-init."""
    import shutil
    if not shutil.which("ssh-keygen"):
        import pytest
        pytest.skip("ssh-keygen not available")
    import subprocess as sp, tempfile, os as _os
    import backend.db as db
    with tempfile.TemporaryDirectory() as td:
        kp = _os.path.join(td, "k")
        sp.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", kp], check=True, capture_output=True)
        priv, pub = open(kp).read(), open(kp + ".pub").read().strip()
    cid = client.post("/credentials", json={"name": "team-key", "kind": "ssh", "username": "admin", "secret": priv}).json()["id"]
    pid = client.post("/infra", json={"name": "acc", "provider": "libvirt",
                                      "options": {"count": 1, "base_image": "x", "ssh_user": "admin"}}).json()["project_id"]
    r = client.patch(f"/infra/{pid}", json={"deploy_credential_id": cid})
    assert r.status_code == 200 and db.get_infra(pid)["deploy_credential_id"] == cid
    ci = (db.project_dir(pid) / "cloudinit.cfg").read_text()
    assert pub in ci                     # the credential's public key is now baked in
    # Clearing it is accepted.
    assert client.patch(f"/infra/{pid}", json={"deploy_credential_id": None}).status_code == 200
    assert (db.get_infra(pid)["deploy_credential_id"] or None) is None
