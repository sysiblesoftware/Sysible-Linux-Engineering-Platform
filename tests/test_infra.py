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
