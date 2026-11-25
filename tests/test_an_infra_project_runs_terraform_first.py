"""A project that builds infrastructure must not default its run to Ansible.

Reported as "Pipelines default to ansible when it should be terraform if we're
building infra", against a project holding main.tf / variables.tf / outputs.tf
and no playbook at all. Step 1 came up Ansible, and because the target picker
lists only .yml/.yaml it collapsed to an empty free-text box prompting for
"playbook.yml" -- a file the project does not contain and never will.

Running a playbook first is also the wrong order on its own terms: it targets
hosts that do not exist until apply has run. The modal's own hint says so --
"a common pipeline is build (Terraform/OpenTofu) -> configure (Ansible)".

Two signals decide it, because either can be absent:
  * the project is registered infrastructure (the record the rest of the infra
    UI reads), which is authoritative; and
  * the project contains .tf files, which covers hand-written Terraform that
    was never registered.

Only ever for a FRESH run or pipeline. A saved pipeline is whatever its author
made it.
"""
import re
from pathlib import Path

IDE = (Path(__file__).resolve().parents[1]
       / "webgui" / "frontend" / "src" / "views" / "Ide.jsx").read_text(encoding="utf-8")


def _component(name):
    """The source of one exported component.

    Both modals carry near-identical effects, so every assertion below is scoped
    to one of them. A repo-wide search would pass on either copy and prove
    nothing about the other -- which is how they came to differ.
    """
    start = IDE.index(f"export function {name}")
    nxt = [i for i in (IDE.find("\nexport function ", start + 1),
                       IDE.find("\nfunction ", start + 1)) if i != -1]
    return IDE[start:min(nxt)] if nxt else IDE[start:]


PIPELINE = _component("PipelineModal")
RUN = _component("RunModal")


def test_the_pipeline_switches_to_terraform_for_an_infra_project():
    assert "startWithTerraform" in PIPELINE, \
        "nothing makes a fresh pipeline start with Terraform in an infra project"
    m = re.search(r"const startWithTerraform = \(\) => \{(.+?)\n  \}", PIPELINE, re.S)
    assert m, "startWithTerraform is not defined in PipelineModal"
    assert "'terraform'" in m.group(1), \
        f"startWithTerraform does not actually select Terraform: {m.group(1)!r}"


def test_the_pipelines_terraform_step_applies_rather_than_asking_for_a_playbook():
    """target has to become an action. Left as 'site.yml' it would be a
    Terraform step pointed at a playbook."""
    m = re.search(r"const startWithTerraform = \(\) => \{(.+?)\n  \}", PIPELINE, re.S)
    assert "'apply'" in m.group(1), f"the first step is not an apply: {m.group(1)!r}"


def test_both_signals_are_wired_up():
    """Registered infrastructure AND bare .tf files. Only one of them was ever
    going to be present for a hand-written Terraform project."""
    assert PIPELINE.count("startWithTerraform()") >= 2, (
        "only one signal selects Terraform; a project with .tf files that was "
        "never registered as infrastructure would still default to Ansible")
    assert re.search(r"endsWith\('\.tf'\)", PIPELINE), \
        "nothing looks for .tf files"
    assert "(f.infra || [])" in PIPELINE, \
        "nothing consults the registered-infrastructure record"


def test_a_saved_pipeline_is_never_second_guessed():
    """An edit or a re-open must come back exactly as it was saved."""
    m = re.search(r"const freshPipeline = useRef\(([^\n]*)\)", PIPELINE)
    assert m, "nothing distinguishes a fresh pipeline from a saved one"
    assert "initialSteps" in m.group(1), \
        f"the guard does not key on initialSteps: {m.group(1)!r}"
    body = re.search(r"const startWithTerraform = \(\) => \{(.+?)\n  \}", PIPELINE, re.S).group(1)
    assert "if (!freshPipeline.current) return" in body, \
        "startWithTerraform does not bail out for a saved pipeline"


def test_an_ansible_step_is_not_pre_filled_with_a_terraform_file():
    """currentFile is whatever the IDE had open. With main.tf focused, the old
    default put main.tf in an Ansible step's target -- a file the picker does
    not even list."""
    m = re.search(r"const blank = \(over\) => \(\{(.+?)\}\)", PIPELINE, re.S)
    assert m, "blank() not found"
    assert re.search(r"ya\?ml", m.group(1)), (
        "blank() still takes currentFile as an Ansible target without checking "
        f"that it is a playbook: {m.group(1)!r}")


def test_the_single_run_modal_does_the_same_thing():
    """The Run button sits beside Pipeline on the same bar and had the same
    default. Fixing one and not the other would be odd to use."""
    assert "startWithTerraform" in RUN, \
        "the Run modal still defaults an infra project to Ansible"
    assert RUN.count("startWithTerraform()") >= 2, \
        "the Run modal wires up only one of the two signals"
    m = re.search(r"const freshEngine = useRef\(([^\n]*)\)", RUN)
    assert m and "ini.engine" in m.group(1), \
        "a Run prefilled with an engine (a re-run) would be overridden"
    # And the helper must actually select it. Asserting only that the function
    # exists and is called lets an empty one pass -- which it did, until a
    # mutation emptied it and every assertion here still held.
    body = re.search(r"const startWithTerraform = \(\) => \{(.+?)\n  \}", RUN, re.S)
    assert body and "'terraform'" in body.group(1), \
        f"the Run modal's helper does not select Terraform: {body and body.group(1)!r}"
