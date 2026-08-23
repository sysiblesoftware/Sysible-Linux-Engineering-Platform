"""Rendered Ansible inventory must be parseable — regression for group names
with spaces (a Controller environment like "Sysible Labs" produced an invalid
[Sysible Labs] section, so no hosts matched)."""
from pathlib import Path

from backend.runners.ansible_runner import _ansible_group, _render_inventory


def test_group_names_are_sanitized():
    assert _ansible_group("Sysible Labs") == "Sysible_Labs"
    assert _ansible_group("prod-web") == "prod_web"
    assert _ansible_group("  spaced  ") == "spaced"
    assert _ansible_group("123nums") == "g_123nums"   # can't start with a digit
    assert _ansible_group("") == "ungrouped"


def test_rendered_inventory_has_valid_sections(tmp_path):
    hosts = [
        {"name": "arch-01", "address": "192.168.100.201", "groups": "Sysible Labs", "variables": {}},
        {"name": "ubuntu-01", "address": "192.168.100.59", "groups": "Sysible Labs", "variables": {}},
    ]
    dest = tmp_path / "inv.ini"
    _render_inventory(hosts, {"username": "admin"}, dest)
    text = dest.read_text()
    assert "[Sysible_Labs]" in text and "[Sysible Labs]" not in text

    # It actually parses as an Ansible inventory (both hosts land in the group).
    from configparser import ConfigParser
    cp = ConfigParser(allow_no_value=True, delimiters=("\n",))
    cp.read_string(text)
    assert "Sysible_Labs" in cp
    body = "\n".join(k for k in cp["Sysible_Labs"])
    assert "arch-01" in body and "ubuntu-01" in body


def test_bastion_without_key_falls_back_to_proxyjump(tmp_path):
    hosts = [{"name": "web1", "address": "10.0.0.11", "groups": "", "variables": {}}]
    dest = tmp_path / "inv.ini"
    _render_inventory(hosts, {"username": "admin"}, dest, bastion="ops@192.168.8.212")
    text = dest.read_text()
    assert "[all:vars]" in text
    assert "ansible_ssh_common_args=-o ProxyJump=ops@192.168.8.212" in text


def test_target_that_is_the_bastion_connects_directly(tmp_path):
    """A host whose address is the jump host must not jump through itself — it
    lands in [slep_direct] with vars that override the [all:vars] ProxyCommand."""
    hosts = [
        {"name": "virt", "address": "192.168.8.212", "groups": "Dev", "variables": {}},
        {"name": "web1", "address": "10.0.0.11", "groups": "Dev", "variables": {}},
    ]
    dest = tmp_path / "inv.ini"
    _render_inventory(hosts, {"username": "admin"}, dest,
                      bastion="admin@192.168.8.212", bastion_key="/data/ssh/slep_ed25519")
    text = dest.read_text()
    assert "[slep_direct]\nvirt\n" in text
    assert "[slep_direct:vars]" in text
    # web1 is not direct; only virt is in the direct group.
    direct = text.split("[slep_direct]\n", 1)[1].split("[slep_direct:vars]")[0]
    assert "web1" not in direct


def test_bastion_with_key_uses_explicit_proxycommand(tmp_path):
    """With SLEP's managed key, the jump hop is an explicit ProxyCommand that keys
    into the bastion and disables host-key checks on that hop — native ProxyJump
    doesn't reliably inherit those, causing 'Connection closed by UNKNOWN'."""
    hosts = [{"name": "web1", "address": "10.0.0.11", "groups": "", "variables": {}}]
    dest = tmp_path / "inv.ini"
    _render_inventory(hosts, {"username": "admin"}, dest,
                      bastion="ops@192.168.8.212", bastion_key="/data/ssh/slep_ed25519")
    text = dest.read_text()
    assert 'ProxyCommand="ssh ' in text and "-i /data/ssh/slep_ed25519" in text
    assert "-W %h:%p ops@192.168.8.212" in text
    assert "StrictHostKeyChecking=no" in text


def test_no_bastion_section_when_unset(tmp_path):
    hosts = [{"name": "web1", "address": "10.0.0.11", "groups": "", "variables": {}}]
    dest = tmp_path / "inv.ini"
    _render_inventory(hosts, {"username": "admin"}, dest)
    assert "[all:vars]" not in dest.read_text()


def test_wait_for_ssh_short_circuits(monkeypatch):
    """The boot-readiness wait must never delay or hang when there's nothing to
    probe: a configured jump host (probe path differs) or hosts without an address
    both return immediately, emitting nothing."""
    import backend.runners.ansible_runner as ar
    lines = []
    emit = lines.append
    # Jump host set → skip (return at once).
    ar._wait_for_ssh([{"name": "a", "address": "10.0.0.1"}], emit, timeout=1, bastion="user@jump")
    # No usable addresses → skip.
    ar._wait_for_ssh([{"name": "a", "address": ""}], emit, timeout=1)
    assert lines == []
