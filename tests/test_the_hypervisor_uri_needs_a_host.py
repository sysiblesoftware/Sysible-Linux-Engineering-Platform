"""The hypervisor connection panel, and the two ways it misled an operator.

REPORTED, with a screenshot of Build infrastructure → libvirt → Remote KVM host
over SSH:

    Connection URI (assembled — editable)
    qemu+ssh://admin@/system?no_verify=1

    error: failed to connect to the hypervisor
    error: Cannot recv data: ssh: connect to host localhost port 22:
    Connection refused

...and then, from the operator: "I didn't put the ip in. That IP should be
blank because it's confusing and looks like it's already in there."

BUG ONE — the placeholder. `Host (IP or name)` carried `placeholder=
"192.168.8.212"`: a bare address, in placeholder grey, which at a glance is
indistinguishable from a filled-in field. It was also a REAL address from the
author's own network, so the field read as "already configured, pointing at a
machine you have never heard of". The host was in fact empty the whole time.

BUG TWO — and this is why the error was so confusing. With the host empty the
panel assembled `qemu+ssh://admin@/system?no_verify=1` anyway and offered it as
a working connection. libvirt cannot parse a URI with no host, falls back to its
default, and tries LOCALHOST — a machine the operator never named. "The
hypervisor is down" is the obvious reading; "you have not told me which
hypervisor" is the truth.

The URI is empty until there is a host to put in it. Test hypervisor connection,
Load from host and Load images are all already disabled on `!values.uri`, so
the controls that produced that error cannot be pressed at all.

buildUri is EXECUTED here, under node, rather than matched as text — the bug was
in what the expression produced, which no amount of reading it caught.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
INFRA = (SRC / "views" / "Infrastructure.jsx").read_text(encoding="utf-8")
NODE = shutil.which("node") or "/opt/node22/bin/node"


def _build_uri():
    """Lift buildUri out of the component and hand it to node.

    Plain JS once the optional-chaining is left alone (node understands it),
    so it runs exactly as the browser runs it.
    """
    m = re.search(r"const buildUri = \(m, h, u, k\) => \{.*?\n  \}", INFRA, re.S)
    assert m, "buildUri is not where this test thinks it is"
    return m.group(0)


def _call(mode, host, user, key=None):
    script = (_build_uri() + "\n"
              + "const out = buildUri(%s, %s, %s, %s);\n"
                "process.stdout.write(JSON.stringify(out));"
              % (json.dumps(mode), json.dumps(host), json.dumps(user),
                 json.dumps(key)))
    r = subprocess.run([NODE, "--input-type=module", "-e", script],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---------------------------------------------------------------------------
# no host, no URI
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["", "   ", "\t"])
def test_a_remote_hypervisor_with_no_host_has_no_uri(host):
    assert _call("remote", host, "admin") == "", (
        "it still assembles a URI with nothing to connect to, which libvirt "
        "resolves to localhost")


def test_it_does_not_produce_the_uri_from_the_report():
    """The exact string on the screenshot."""
    got = _call("remote", "", "admin")
    assert "@/system" not in got, got
    assert got != "qemu+ssh://admin@/system?no_verify=1"


def test_a_host_that_is_only_whitespace_is_no_host():
    assert _call("remote", "  ", "root") == ""


# ---------------------------------------------------------------------------
# ...and with one, the URI is the one libvirt wants
# ---------------------------------------------------------------------------
def test_a_remote_hypervisor_with_a_host_assembles_properly():
    got = _call("remote", "10.0.0.5", "admin")
    assert got == "qemu+ssh://admin@10.0.0.5/system?no_verify=1", got


def test_the_user_defaults_to_root():
    assert _call("remote", "10.0.0.5", "") == \
        "qemu+ssh://root@10.0.0.5/system?no_verify=1"


def test_the_installed_key_is_referenced_when_there_is_one():
    got = _call("remote", "10.0.0.5", "admin", {"keyfile": "/etc/slep/id_ed25519"})
    assert "keyfile=/etc/slep/id_ed25519" in got, got
    assert got.startswith("qemu+ssh://admin@10.0.0.5/system?"), got


def test_local_needs_no_host_at_all():
    """A local hypervisor is qemu:///system whatever the host box says."""
    assert _call("local", "", "") == "qemu:///system"
    assert _call("local", "10.0.0.5", "admin") == "qemu:///system"


# ---------------------------------------------------------------------------
# the placeholder that read as a value
# ---------------------------------------------------------------------------
def test_the_host_field_has_no_placeholder_address():
    m = re.search(r'label="Host \(IP or name\)".*?/>', INFRA, re.S)
    assert m, "the host field is not where this test thinks it is"
    assert "placeholder" not in m.group(0), (
        "the host field offers a placeholder again — a bare address in grey "
        "reads as a value that is already there")


def test_no_real_address_is_left_anywhere_in_the_ui():
    """It was the author's own hypervisor, in five places. Examples belong in
    the documentation ranges (RFC 5737), which can never be a real host."""
    bad = []
    for f in SRC.rglob("*.jsx"):
        if "192.168.8.212" in f.read_text(encoding="utf-8"):
            bad.append(f.name)
    assert not bad, f"a real network address is still shown to operators: {bad}"


def test_the_jump_host_fields_do_not_offer_a_bare_address_either():
    """Same bug, same reason: `user@host` reads as an example because of its
    shape; a bare address does not."""
    for f in SRC.rglob("*.jsx"):
        text = f.read_text(encoding="utf-8")
        for ph in re.findall(r'placeholder="([^"]*)"', text):
            if re.fullmatch(r"\s*\d{1,3}(\.\d{1,3}){3}\s*", ph):
                pytest.fail(f"{f.name} offers a bare address as a placeholder: {ph!r}")
