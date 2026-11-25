"""A cloud image is built for ONE machine architecture, and nothing checked it.

The catalog shipped eleven images, every one of them x86_64, and said so only in
a source comment. The hypervisor URI — which is the only thing that says where
the VM actually runs, and may be a remote machine of any architecture — was
never consulted. On an aarch64 hypervisor the operator picked "Ubuntu 24.04
LTS", the image downloaded fine, the domain was created, and then it simply
never booted: no console output, no installer, no error from the download, and
nothing anywhere naming the architecture. It looks exactly like a corrupt image.

The guard fails closed ONLY on a mismatch it is sure of. Both sides have to
state an architecture and they have to disagree, because a hypervisor that
merely cannot be probed is not a hypervisor that is known to be wrong — and
blocking on "unknown" would break every working setup where virsh isn't
installed on the SLEP host.
"""
import pytest

from backend import infra


# ---------------------------------------------------------------- the catalog

def test_every_catalog_image_says_which_machine_it_is_for():
    assert infra.CLOUD_IMAGES
    for c in infra.CLOUD_IMAGES:
        assert c.get("arch") in ("x86_64", "aarch64"), \
            f"{c['label']} does not say which machine it is for: {c}"


def test_the_catalog_offers_something_for_a_non_x86_hypervisor():
    """It was x86_64-only. An aarch64 operator had nothing to pick at all."""
    assert any(c["arch"] == "aarch64" for c in infra.CLOUD_IMAGES), \
        "nothing in the catalog can boot on an aarch64 hypervisor"


def test_a_catalog_entrys_stated_arch_matches_its_own_url():
    """A label saying aarch64 over an amd64 URL would be worse than no label."""
    for c in infra.CLOUD_IMAGES:
        got = infra.image_arch(c["url"])
        assert got == c["arch"], \
            f"{c['label']} is labelled {c['arch']} but its URL is {got or 'unmarked'}: {c['url']}"


# ------------------------------------------------------------- reading a URL

@pytest.mark.parametrize("url,expect", [
    ("https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img", "x86_64"),
    ("https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img", "aarch64"),
    ("https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-arm64.qcow2", "aarch64"),
    ("https://repo.almalinux.org/almalinux/9/cloud/aarch64/images/Alma.aarch64.qcow2", "aarch64"),
    ("https://download.rockylinux.org/pub/rocky/9/images/x86_64/R.latest.x86_64.qcow2", "x86_64"),
    # An operator's own URL, and a pool volume name: neither states an arch.
    ("https://files.example.com/golden/base.qcow2", ""),
    ("jammy.qcow2", ""),
    ("", ""),
])
def test_the_arch_is_read_out_of_the_url(url, expect):
    assert infra.image_arch(url) == expect


def test_a_url_that_contradicts_itself_is_not_guessed_at():
    """A mirror path that disagrees with the filename. Picking either one could
    block a legitimate image, and the guard's whole licence to fail closed is
    that it is only ever sure."""
    assert infra.image_arch(
        "https://mirror.example/amd64/debian-12-generic-arm64.qcow2") == ""


def test_both_spellings_of_each_machine_are_the_same_machine():
    """virsh nodeinfo says x86_64; a Debian image URL says amd64. Comparing the
    two without normalizing is how this check would silently never fire."""
    assert infra.normalize_arch("amd64") == infra.normalize_arch("x86_64") == "x86_64"
    assert infra.normalize_arch("arm64") == infra.normalize_arch("aarch64") == "aarch64"
    assert infra.normalize_arch("AArch64") == "aarch64"
    assert infra.normalize_arch("riscv64") == ""
    assert infra.normalize_arch("") == ""


# ------------------------------------------------- the hypervisor is asked

def _virsh(monkeypatch, cpu_model, rc=0):
    """Make the SLEP host look like it has virsh, reporting `cpu_model`."""
    import backend.app as appmod
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/virsh")

    def fake_run(cmd, *a, **k):
        class R:
            returncode = rc
            stderr = ""
            stdout = (f"CPU model:           {cpu_model}\nCPU(s):              8\n"
                      if "nodeinfo" in cmd else "ok\n")
        return R()
    monkeypatch.setattr(appmod.subprocess, "run", fake_run)


def test_the_hypervisor_is_asked_what_it_is_not_the_slep_host(monkeypatch):
    """SLEP drives remote hypervisors over qemu+ssh://. Using the SLEP host's own
    platform.machine() would answer a different question entirely — and get it
    wrong in exactly the case the check exists for."""
    import backend.app as appmod
    _virsh(monkeypatch, "aarch64")
    assert appmod._hypervisor_arch("qemu+ssh://root@kvm-host/system") == "aarch64"


def test_an_unprobeable_hypervisor_reads_as_unknown_not_as_x86(monkeypatch):
    import backend.app as appmod
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: None)     # no virsh installed
    assert appmod._hypervisor_arch("qemu:///system") == ""
    _virsh(monkeypatch, "aarch64", rc=1)                     # virsh ran, failed
    assert appmod._hypervisor_arch("qemu:///system") == ""
    _virsh(monkeypatch, "")                                  # unparseable output
    assert appmod._hypervisor_arch("qemu:///system") == ""


# ----------------------------------------------------- the wizard refuses it

ARM_URI = "qemu+ssh://root@arm-host/system"
X86_IMG = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
ARM_IMG = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"


def _create(client, **options):
    return client.post("/infra", json={
        "name": "arch-check-" + str(abs(hash(str(options))) % 100000),
        "provider": "libvirt",
        "options": {"count": 1, "ssh_user": "ubuntu", **options},
    })


def test_an_x86_image_on_an_arm_hypervisor_is_refused(client, monkeypatch):
    _virsh(monkeypatch, "aarch64")
    r = _create(client, uri=ARM_URI, base_image=X86_IMG)
    assert r.status_code == 400, r.text
    d = r.json()["detail"]
    assert "aarch64" in d and "x86_64" in d, d
    assert "never boot" in d, f"the message does not say what goes wrong: {d}"


def test_a_matching_image_is_allowed(client, monkeypatch):
    _virsh(monkeypatch, "aarch64")
    r = _create(client, uri=ARM_URI, base_image=ARM_IMG)
    assert r.status_code == 200, r.text


def test_the_reverse_mismatch_is_refused_too(client, monkeypatch):
    """Not a check that only ever runs in one direction."""
    _virsh(monkeypatch, "x86_64")
    r = _create(client, uri="qemu:///system", base_image=ARM_IMG)
    assert r.status_code == 400, r.text
    assert "x86_64" in r.json()["detail"]


def test_one_wrong_group_among_several_is_refused(client, monkeypatch):
    """Multi-group libvirt: a single bad group is still a VM that never boots."""
    _virsh(monkeypatch, "aarch64")
    r = _create(client, uri=ARM_URI, groups=[
        {"base_image": ARM_IMG, "count": 1},
        {"base_image": X86_IMG, "count": 1},
    ])
    assert r.status_code == 400, r.text
    assert "x86_64" in r.json()["detail"]


def test_an_unprobeable_hypervisor_does_not_block_the_operator(client, monkeypatch):
    """The commonest real setup: no virsh on the SLEP host. Unknown must not
    become a refusal, or this 'fix' breaks every working install."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: None)
    r = _create(client, uri=ARM_URI, base_image=X86_IMG)
    assert r.status_code == 200, r.text


def test_an_image_url_with_no_arch_in_it_does_not_block(client, monkeypatch):
    """A pool volume or an operator's own golden image states no arch. Guessing
    would refuse something that is very likely fine."""
    _virsh(monkeypatch, "aarch64")
    r = _create(client, uri=ARM_URI, base_image="https://files.example.com/golden/base.qcow2")
    assert r.status_code == 200, r.text


def test_the_connection_test_tells_the_operator_the_machine(client, monkeypatch):
    """Otherwise there is no way to find out short of guessing."""
    _virsh(monkeypatch, "aarch64")
    d = client.post("/infra/test-hypervisor", json={"uri": ARM_URI}).json()
    assert d["ok"] is True, d
    assert "aarch64" in d["output"], d["output"]
