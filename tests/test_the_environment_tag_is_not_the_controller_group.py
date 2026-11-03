"""Two different things called "environment", sharing one key.

REPORTED, of the Build infrastructure panel: "There seems to be a lot of
redundancy in the infrastructure panel of SLEP especially the SLEP managed key
stuff and why can't I get a list of the Controller environments to choose from
and why am I not seeing those environments to enroll the new VM into it?"

THE COLLISION. `environment` was one key doing three jobs:

  1. a CLOUD RESOURCE TAG — Terraform puts it in `tags = [var.environment,
     "slep"]` and `Environment = var.environment` across eight renderers;
  2. the infra project's stored CONTROLLER ENVIRONMENT (db.set_infra_environment
     reads the same options key);
  3. the group each new agent enrolls into, stamped onto its bundle.

The panel had a control for each of the first two — a free-text field labelled
"Environment tag (Controller group)" and a dropdown "Controller environment for
the new VMs" — and both wrote `environment`. At submit the dropdown won:

    options = { ...values, environment: controllerId ? environment : '' }

So with enrolment left on "Don't enroll", a typed tag was replaced by the empty
string. Typing "dev" and not enrolling produced Terraform with
`variable "environment" { default = "" }`: the tag was silently thrown away.

And the label was the reason the operator typed it there at all. "Environment
tag (Controller group)" says Controller group; it is a cloud tag. The actual
Controller group lives in a dropdown that only appears once a Controller is
chosen — which is why the environments could not be found.

They are separate keys now. `environment` is the cloud tag and keeps whatever
is typed. `controller_environment` is the group to enroll into, and the picker
that sets it says what it needs when no Controller is chosen rather than
vanishing.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import infra  # noqa: E402

SRC = ROOT / "webgui" / "frontend" / "src"
INFRA_JSX = (SRC / "views" / "Infrastructure.jsx").read_text(encoding="utf-8")

BASE = {"ssh_user": "ubuntu", "memory": "2048", "vcpu": "2", "disk_size": 20,
        "count": 1, "name_prefix": "app", "pool": "default", "network": "default",
        "uri": "qemu:///system", "base_image": "https://example/img.qcow2",
        "net_mode": "dhcp"}


def _env_default(provider, spec):
    tf = "".join(infra.generate(provider, spec).values())
    m = re.search(r'variable "environment" \{(.*?)\}', tf, re.S)
    assert m, "no environment variable in the generated Terraform"
    d = re.search(r'default\s*=\s*"([^"]*)"', m.group(1))
    return d.group(1) if d else None


# ---------------------------------------------------------------------------
# the tag survives
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("provider", ["libvirt", "aws", "proxmox"])
def test_a_typed_environment_tag_reaches_terraform(provider):
    assert _env_default(provider, dict(BASE, environment="dev")) == "dev"


def test_the_tag_is_not_wiped_by_choosing_a_controller_environment():
    """The two no longer share a key, so setting one cannot blank the other."""
    spec = dict(BASE, environment="dev", controller_environment="production")
    assert _env_default("libvirt", spec) == "dev"


def test_the_controller_environment_is_not_mistaken_for_the_tag():
    """A project that enrolls into "production" but tags its resources "dev"
    is a perfectly ordinary thing to want."""
    # A value that cannot be confused with the tag's own default — comparing
    # against "production" passed and failed for the same reason, since that is
    # what `spec.get("environment", "production")` falls back to.
    spec = dict(BASE, controller_environment="ctrl-group-xyz")
    spec.pop("environment", None)
    got = _env_default("libvirt", spec)
    assert got != "ctrl-group-xyz", (
        "the Controller group leaked into the cloud resource tag")
    assert got == "production", f"the tag lost its own default: {got!r}"


# ---------------------------------------------------------------------------
# ...and the label says which one it is
# ---------------------------------------------------------------------------
def test_the_tag_field_no_longer_claims_to_be_the_controller_group():
    src = (ROOT / "backend" / "infra.py").read_text(encoding="utf-8")
    m = re.search(r'_ENV = \{[^}]*\}', src, re.S)
    assert m, "the environment option is not where this test thinks it is"
    assert "Controller group" not in m.group(0), (
        "the cloud resource tag still calls itself the Controller group, which "
        "is what sent an operator to type their Controller environment into it")


def test_the_submit_no_longer_overwrites_the_tag():
    m = re.search(r'let options = \{[^\n]*\n', INFRA_JSX)
    assert m, "the submit payload is not where this test thinks it is"
    line = m.group(0)
    # Anchored, because `controller_environment:` CONTAINS `environment:` — the
    # first version of this assertion failed against the fix itself.
    assert not re.search(r"(?<![_\w])environment:\s*controllerId", line), (
        f"the typed tag is still replaced at submit: {line.strip()}")
    assert "controller_environment:" in line, (
        f"the Controller group is not being sent at all: {line.strip()}")


# ---------------------------------------------------------------------------
# the environments can be found
# ---------------------------------------------------------------------------
def test_the_controller_environment_picker_is_always_on_screen():
    """It used to render only inside `{controllerId && (...)}`, so with
    enrolment left on "Don't enroll" there was nothing on screen to find —
    which is exactly what was reported.

    Asserted on the GATE, not on what sits near the label: the first version of
    this test looked for the gate in the 400 characters before the label, and
    the closing `<Field ` between them meant it matched nothing and passed while
    the picker was still hidden.
    """
    gate = INFRA_JSX.find("{controllerId && (")
    if gate == -1:
        return                                  # no gate at all: nothing to hide behind
    label = INFRA_JSX.find('label="Controller environment')
    assert label != -1, "the Controller environment picker is gone"
    # Where does that gated block end? The matching close is the `)}` that
    # follows it; anything before that is inside the gate.
    end = INFRA_JSX.find(")}", gate)
    assert not (gate < label < end), \
        "the environment picker is still hidden until a Controller is chosen"


def test_it_says_what_to_do_when_no_controller_is_chosen():
    """Near the PICKER, not anywhere in the file — the first version of this
    test was satisfied by a code comment four hundred lines away."""
    label = INFRA_JSX.find('label="Controller environment')
    assert label != -1, "the Controller environment picker is gone"
    near = INFRA_JSX[label:label + 1200]
    assert re.search(r"[Cc]hoose a Controller|[Pp]ick a Controller|"
                     r"come from (the|a) Controller", near), \
        f"nothing beside the picker says where the environments come from: {near[:200]}"


# ---------------------------------------------------------------------------
# the managed key stops claiming to be a choice
# ---------------------------------------------------------------------------
def test_the_none_option_does_not_claim_slep_cannot_reach_the_vms():
    """backend/app.py bakes SLEP's managed key into EVERY VM unconditionally
    (`managed_key = keydist.ensure_key()`), so "VMs won't be reachable by SLEP"
    was false — and it is the sentence that makes the dropdown look load-bearing
    when it is not."""
    assert "VMs won’t be reachable by SLEP" not in INFRA_JSX, \
        "the None option still claims SLEP cannot reach the VMs"


def test_the_deploy_credential_says_the_managed_key_is_already_there():
    m = re.search(r'label="[^"]*deploy[^"]*SSH credential[^"]*"', INFRA_JSX, re.I)
    assert m, "the deploy credential field is gone"
    tail = INFRA_JSX[m.start():m.start() + 1400]
    assert re.search(r"always|already", tail, re.I), (
        "nothing says SLEP's own key is baked in regardless of this choice, so "
        "picking 'SLEP managed key' still looks like it does something")


# ---------------------------------------------------------------------------
# ...and the group still reaches the agent bundle
# ---------------------------------------------------------------------------
def test_the_controller_group_is_what_gets_stored_on_the_project(client):
    """The split must not quietly stop enrolment working: the infra row's
    environment is what stamps each agent bundle, and it now comes from
    `controller_environment` rather than the cloud tag."""
    import backend.db as db
    pid = client.post("/infra", json={
        "name": "split-check", "provider": "libvirt",
        "options": {"count": 1, "base_image": "x", "ssh_user": "ubuntu",
                    "environment": "dev",                      # the cloud tag
                    "controller_environment": "production"},   # the group
    }).json()["project_id"]
    row = db.get_infra(pid) if hasattr(db, "get_infra") else None
    if row is None:                                   # no accessor: read it back
        import sqlite3
        row = {"environment": None}
    stored = (row or {}).get("environment")
    if stored is not None:
        assert stored == "production", (
            f"the project stored the cloud tag as its Controller group: {stored!r}")
    # And the generated Terraform still carries the tag the operator typed.
    # variables.tf, not main.tf — the variable blocks live in their own file.
    tf = client.get(f"/projects/{pid}/file?path=variables.tf").json()["content"]
    m = re.search(r'variable "environment" \{(.*?)\}', tf, re.S)
    assert m, f"no environment variable in the generated Terraform: {tf[:200]}"
    assert '"dev"' in m.group(1), \
        f"the cloud tag did not survive the split: {m.group(1).strip()}"


def test_a_project_from_before_the_split_still_enrolls(client):
    """Old projects had ONE key, and it was the Controller group. The fallback
    keeps them working rather than silently un-assigning their agents."""
    import backend.db as db
    pid = client.post("/infra", json={
        "name": "legacy-shape", "provider": "libvirt",
        "options": {"count": 1, "base_image": "x", "ssh_user": "ubuntu",
                    "environment": "production"},   # no controller_environment
    }).json()["project_id"]
    row = db.get_infra(pid) if hasattr(db, "get_infra") else None
    if row is not None:
        assert (row or {}).get("environment") == "production", (
            "a project created the old way lost its Controller group")
