"""An Ansible step whose target cannot be a playbook is refused at launch.

Run #144: the pipeline's Ansible step was pointed at `main.tf`. An Ansible
step's target IS the playbook path (ansible_runner resolves `workdir /
run["target"]`), so that ran `ansible-playbook main.tf`, which dies at parse,
emits zero tasks, and leaves a run holding nothing that explains itself.

The runner already refused a path that escapes the project or does not exist.
What it could not tell was that an existing, readable file is not a playbook.
That check now happens at LAUNCH, where the operator is still looking -- and on
the schedule path too, for the same reason the job-template check beside it
exists: a bad target discovered at 02:00 is discovered by nobody.

The hard part is not catching main.tf; it is not catching anything else. These
tests spend most of their length on what must STILL be allowed.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.runners.ansible_runner import why_not_a_playbook   # noqa: E402


# ---- what must be refused --------------------------------------------------
def test_the_terraform_file_that_caused_the_report(tmp_path):
    (tmp_path / "main.tf").write_text(
        'resource "libvirt_domain" "repo-1" {\n  name = "repo-1"\n  memory = 2048\n}\n')
    why = why_not_a_playbook(tmp_path, "main.tf")
    assert why and "main.tf" in why
    assert "playbook" in why.lower()


def test_a_mapping_is_not_a_list_of_plays(tmp_path):
    """Valid YAML, wrong shape -- a group_vars file or an inventory, pointed at
    by mistake. ansible-playbook rejects it just as hard."""
    (tmp_path / "vars.yml").write_text("http_port: 80\nmax_clients: 200\n")
    why = why_not_a_playbook(tmp_path, "vars.yml")
    assert why and "list of plays" in why


def test_an_empty_file(tmp_path):
    (tmp_path / "empty.yml").write_text("")
    assert why_not_a_playbook(tmp_path, "empty.yml")


def test_a_file_of_only_comments(tmp_path):
    (tmp_path / "todo.yml").write_text("# write this later\n")
    assert why_not_a_playbook(tmp_path, "todo.yml")


def test_a_directory(tmp_path):
    (tmp_path / "roles").mkdir()
    why = why_not_a_playbook(tmp_path, "roles")
    assert why and "directory" in why


def test_a_path_that_escapes_the_project(tmp_path):
    """The runner's guard, moved earlier. The target comes from the console."""
    why = why_not_a_playbook(tmp_path, "../../etc/passwd")
    assert why and "outside" in why


def test_nothing_at_all(tmp_path):
    assert why_not_a_playbook(tmp_path, "")


# ---- what must STILL be allowed --------------------------------------------
def test_an_ordinary_playbook(tmp_path):
    (tmp_path / "site.yml").write_text(
        "- hosts: all\n  tasks:\n    - name: ping\n      ansible.builtin.ping:\n")
    assert why_not_a_playbook(tmp_path, "site.yml") is None


def test_a_playbook_that_does_not_end_in_yml(tmp_path):
    """ansible-playbook runs any readable YAML whatever it is called. A check
    that insisted on an extension would reject working setups to catch a typo."""
    (tmp_path / "deploy").write_text("- hosts: web\n  roles: [nginx]\n")
    assert why_not_a_playbook(tmp_path, "deploy") is None


def test_a_playbook_in_a_subdirectory(tmp_path):
    (tmp_path / "plays").mkdir()
    (tmp_path / "plays" / "web.yaml").write_text("- hosts: web\n  tasks: []\n")
    assert why_not_a_playbook(tmp_path, "plays/web.yaml") is None


def test_a_playbook_carrying_an_inline_vault(tmp_path):
    """!vault is not YAML the standard loader knows. Refusing a playbook for
    containing one would be this check causing the thing it exists to prevent."""
    (tmp_path / "secret.yml").write_text(
        "- hosts: all\n  vars:\n    pw: !vault |\n"
        "      $ANSIBLE_VAULT;1.1;AES256\n      3762653...\n  tasks: []\n")
    assert why_not_a_playbook(tmp_path, "secret.yml") is None


def test_a_whole_file_vault_is_not_judged(tmp_path):
    """Encrypted end to end. There is nothing to inspect, so nothing is claimed."""
    (tmp_path / "enc.yml").write_text("$ANSIBLE_VAULT;1.1;AES256\n66386439...\n")
    assert why_not_a_playbook(tmp_path, "enc.yml") is None


def test_a_target_that_does_not_exist_yet_is_left_alone(tmp_path):
    """An earlier step of the same pipeline may write it. The runner reports a
    missing playbook clearly when the time comes; refusing here would break a
    working setup."""
    assert why_not_a_playbook(tmp_path, "generated.yml") is None


def test_a_playbook_using_jinja_in_quoted_values(tmp_path):
    (tmp_path / "t.yml").write_text(
        '- hosts: "{{ target_group }}"\n  tasks:\n'
        '    - name: "deploy {{ app }}"\n      debug: msg="{{ app }}"\n')
    assert why_not_a_playbook(tmp_path, "t.yml") is None


# ---- and it is actually wired in -------------------------------------------
def test_every_launch_path_checks_it():
    """Ad-hoc, pipeline and schedule. A guard on two of the three leaves the
    third as the way in."""
    src = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
    assert src.count("_assert_runnable_playbook(") >= 4, \
        "a launch path no longer validates its playbook"
    # The pipeline check must sit AFTER template expansion, or it validates the
    # template id rather than the playbook the template resolves to.
    disp = src[src.index("def _dispatch_pipeline"):]
    disp = disp[:disp.index("return [rid for rid")]
    assert disp.index('if kind == "template"') < disp.index("_assert_runnable_playbook"), \
        "the pipeline checks the target before the template resolves it"


def test_only_ansible_steps_are_judged():
    """terraform's target is plan/apply/destroy and enroll's is a label. Running
    this check over them would refuse every pipeline."""
    src = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
    body = src[src.index("def _assert_runnable_playbook"):]
    body = body[:body.index("\ndef _dispatch_run")]
    assert 'if kind != "ansible":' in body and "return" in body
