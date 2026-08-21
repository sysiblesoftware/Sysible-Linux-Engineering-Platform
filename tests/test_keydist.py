"""Distribute SSH key: key generation, per-host install (SSH mocked), and the
auto-created 'SLEP managed key' credential."""
import backend.db as db
import backend.keydist as keydist


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _seed_inventory():
    iid = db.create_inventory("keydist-inv")
    db.add_host(iid, "web1", "10.0.0.11", groups="web")
    db.add_host(iid, "web2", "10.0.0.12", groups="web")
    return iid


def test_ensure_key_generates_once(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_run(cmd, **k):
        calls["n"] += 1
        priv = cmd[cmd.index("-f") + 1]
        # ssh-keygen writes both the private and .pub files.
        open(priv, "w").write("PRIVATE")
        open(priv + ".pub", "w").write("ssh-ed25519 AAAAKEY slep-managed")
        return Done()

    monkeypatch.setattr(keydist.subprocess, "run", fake_run)
    monkeypatch.setattr(keydist.shutil, "which", lambda n: "/usr/bin/" + n)
    pub1 = keydist.ensure_key()
    pub2 = keydist.ensure_key()   # reuses, doesn't regenerate
    assert pub1 == pub2 == "ssh-ed25519 AAAAKEY slep-managed"
    assert calls["n"] == 1


def test_distribute_installs_and_creates_credential(client, monkeypatch):
    iid = _seed_inventory()
    monkeypatch.setattr(keydist, "ensure_key", lambda: "ssh-ed25519 AAAAKEY slep-managed")
    monkeypatch.setattr(keydist.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(keydist, "_key_paths", lambda: (_FakePath("PRIVATEKEY"), _FakePath("PUB")))

    seen = []

    def fake_run(cmd, **k):
        seen.append(cmd)
        return Done(0, "SLEP_KEY_OK\n")

    monkeypatch.setattr(keydist.subprocess, "run", fake_run)
    keydist._run_distribute(iid, {"web1", "web2"}, "admin", "pw", "user@bastion")

    # The jump host is prepared first (a direct password hop to it), then each
    # target jumps through it via an explicit ProxyCommand that keys into the
    # bastion (host-key checks disabled on the jump hop too).
    assert any("PubkeyAuthentication=no" in str(x) for c in seen for x in c)
    ssh_cmds = [c for c in seen if any("ProxyCommand=" in str(x) and "user@bastion" in str(x) for x in c)]
    assert len(ssh_cmds) == 2
    # A reusable key credential now exists.
    creds = db.list_credentials()
    mk = [c for c in creds if c["name"] == "SLEP managed key"]
    assert mk and mk[0]["username"] == "admin" and mk[0]["kind"] == "ssh"


def test_hop_for_skips_the_jump_when_target_is_the_bastion():
    assert keydist.bastion_host("admin@192.168.8.212:22") == "192.168.8.212"
    # A normal target still hops through the bastion.
    assert keydist.hop_for("admin@192.168.8.212", "10.0.0.5") == "admin@192.168.8.212"
    # The target that IS the bastion connects directly (no jump).
    assert keydist.hop_for("admin@192.168.8.212", "192.168.8.212") == ""


def test_distribute_route_requires_username(client):
    iid = client.post("/inventories", json={"name": "kd"}).json()["id"]
    r = client.post(f"/inventories/{iid}/distribute-key", json={"password": "pw"})
    assert r.status_code == 400


def test_prepare_bastion_installs_key(monkeypatch):
    monkeypatch.setattr(keydist, "ensure_key", lambda: "ssh-ed25519 AAAAKEY slep-managed")
    monkeypatch.setattr(keydist.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(keydist.subprocess, "run", lambda cmd, **k: Done(0, "SLEP_KEY_OK\n"))
    keydist._run_prepare_bastion("9-bastion", "admin@192.168.8.212", "pw")
    text, _ = keydist.job_log("9-bastion")
    assert "Jump host ready" in text
    assert not keydist.job_running("9-bastion")


def test_prepare_bastion_requires_user_at_host(client):
    iid = client.post("/inventories", json={"name": "b"}).json()["id"]
    r = client.post(f"/inventories/{iid}/prepare-bastion", json={"bastion": "192.168.8.212", "password": "pw"})
    assert r.status_code == 400


def test_connection_test_reports_reachable(client, monkeypatch):
    iid = _seed_inventory()
    monkeypatch.setattr(keydist, "_auth_for", lambda cred: ("key", "/tmp/fake-key", {}, lambda: None))
    monkeypatch.setattr(keydist.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(keydist.subprocess, "run", lambda cmd, **k: Done(0, "SLEP_CONN_OK\n"))
    keydist._run_test(f"{iid}-test", iid, {"web1", "web2"}, None, "user@bastion")
    text, _ = keydist.job_log(f"{iid}-test")
    assert "2 reachable, 0 unreachable" in text


class _FakePath:
    def __init__(self, text):
        self._t = text

    def read_text(self):
        return self._t
