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


def test_bastion_injects_proxyjump(tmp_path):
    hosts = [{"name": "web1", "address": "10.0.0.11", "groups": "", "variables": {}}]
    dest = tmp_path / "inv.ini"
    _render_inventory(hosts, {"username": "admin"}, dest, bastion="ops@192.168.8.212")
    text = dest.read_text()
    assert "[all:vars]" in text
    assert "ansible_ssh_common_args=-o ProxyJump=ops@192.168.8.212" in text


def test_no_bastion_section_when_unset(tmp_path):
    hosts = [{"name": "web1", "address": "10.0.0.11", "groups": "", "variables": {}}]
    dest = tmp_path / "inv.ini"
    _render_inventory(hosts, {"username": "admin"}, dest)
    assert "[all:vars]" not in dest.read_text()
