"""The job options AAP has on a job template, and the idempotence check.

SLEP offered --limit and --start-at-task. Everything else an operator reaches
for — tags, a dry run, diffs, verbosity, --force-handlers — meant editing the
playbook or dropping to a shell on the SLEP host.

--force-handlers is the one that earns its place. A handler runs at the END of a
play, so a playbook that fails after notifying one leaves the service
un-restarted — and the obvious fix, re-running it, does NOT notify again, because
the task that notified is already in the desired state. Without --force-handlers
the only way out is to make a cosmetic change purely to force the notify, which
is the kind of thing people do at 2am and regret.

The idempotence check is the other half: run the playbook a second time and
require that it changes nothing. A playbook that reports changes on every run is
doing work every run — a `command:` with no `creates:`, a template rewriting a
timestamp, a service bounced unconditionally — and on a nightly schedule that is
a restart nobody asked for.

Both halves are pure functions here, so they are tested against the strings
Ansible actually prints rather than against a fleet.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.runners.ansible_runner import (  # noqa: E402
    VERBOSITY_MAX, build_options, changed_in_recap,
)


def argv(**opts):
    a, _notes, err = build_options(opts)
    assert not err, err
    return a


def refusal(**opts):
    a, _notes, err = build_options(opts)
    assert err, f"accepted {opts!r} and produced {a!r}"
    return err


# ---- the handler option ----------------------------------------------------
def test_handlers_can_be_forced_to_run_after_a_failure():
    assert "--force-handlers" in argv(force_handlers=True)


def test_it_is_off_unless_asked_for():
    """It changes failure semantics — handlers fire on a play that FAILED — so it
    is never on by default."""
    assert "--force-handlers" not in argv()
    assert "--force-handlers" not in argv(force_handlers=False)


def test_the_log_says_what_it_will_do():
    _a, notes, _e = build_options({"force_handlers": True})
    assert any("handlers" in n for n in notes), notes


# ---- the rest of the AAP set -----------------------------------------------
def test_tags_and_skip_tags_reach_the_command():
    a = argv(tags="web,db", skip_tags="slow")
    assert a[a.index("--tags") + 1] == "web,db"
    assert a[a.index("--skip-tags") + 1] == "slow"


def test_whitespace_around_tags_is_tidied_not_rejected():
    """`web, db` is what anyone types. Passing it through unchanged makes Ansible
    look for a tag named ' db'."""
    a = argv(tags=" web , db ")
    assert a[a.index("--tags") + 1] == "web,db"


@pytest.mark.parametrize("bad", ["web;rm -rf /", "web db", "--become-user=root", "web,,db", ""])
def test_a_tag_that_is_not_a_tag_is_refused(bad):
    """A mistyped tag selects NO tasks, and the run then reports success having
    done nothing at all. Failing the launch is the kinder answer."""
    if bad == "":
        assert "--tags" not in argv(tags=bad)      # empty just means "no tags"
        return
    assert "comma-separated tag names" in refusal(tags=bad)


def test_check_mode_and_diff():
    assert "--check" in argv(check=True)
    assert "--diff" in argv(diff=True)
    _a, notes, _e = build_options({"check": True})
    assert any("nothing will be changed" in n for n in notes), notes


@pytest.mark.parametrize("v,flag", [(1, "-v"), (2, "-vv"), (4, "-vvvv"), ("3", "-vvv")])
def test_verbosity_becomes_the_right_number_of_vs(v, flag):
    assert flag in argv(verbosity=v)


@pytest.mark.parametrize("v", [5, -1, "lots", 99])
def test_an_impossible_verbosity_is_refused(v):
    assert f"0-{VERBOSITY_MAX}" in refusal(verbosity=v)


def test_zero_verbosity_adds_nothing():
    assert argv(verbosity=0) == []
    assert argv(verbosity="0") == []


def test_the_existing_options_still_work():
    a = argv(limit="web-1", start_at_task="install nginx")
    assert a[a.index("--limit") + 1] == "web-1"
    assert a[a.index("--start-at-task") + 1] == "install nginx"


def test_a_value_can_never_be_read_as_a_flag():
    """Each value lands as its own argv element after its flag, so a value that
    looks like a flag is still a value. This is why the validation above is about
    catching mistakes, not about escaping."""
    a = argv(limit="--become-user=root")
    assert a == ["--limit", "--become-user=root"]
    assert a.index("--limit") == 0


# ---- the idempotence check -------------------------------------------------
def test_idempotence_is_not_a_command_line_flag():
    """It is a second RUN, not a switch. If it ever becomes argv, ansible-playbook
    will reject the whole command."""
    assert argv(idempotence=True) == []


def test_it_says_the_playbook_will_run_twice():
    _a, notes, _e = build_options({"idempotence": True})
    assert any("SECOND time" in n for n in notes), notes


def test_it_is_refused_in_check_mode():
    """A check run changes nothing by definition, so a second pass proves nothing
    — it would report 'idempotent' about a playbook nobody has run."""
    assert "check mode" in refusal(idempotence=True, check=True)


def test_it_is_refused_with_start_at_task():
    """The second pass would skip the very tasks whose idempotence is in question."""
    assert "start at a task" in refusal(idempotence=True, start_at_task="install nginx")


def test_it_is_allowed_with_tags():
    """Narrowing to a tag and checking that part is idempotent is a reasonable
    thing to want."""
    a, _n, err = build_options({"idempotence": True, "tags": "web"})
    assert not err and a == ["--tags", "web"]


# ---- reading the recap -----------------------------------------------------
RECAP_CLEAN = """
PLAY RECAP *********************************************************************
web-1                      : ok=7    changed=0    unreachable=0    failed=0    skipped=1
web-2                      : ok=7    changed=0    unreachable=0    failed=0    skipped=1
"""
RECAP_DIRTY = """
PLAY RECAP *********************************************************************
web-1                      : ok=7    changed=2    unreachable=0    failed=0    skipped=0
web-2                      : ok=7    changed=1    unreachable=0    failed=0    skipped=0
"""


def test_a_clean_second_pass_reads_as_zero():
    assert changed_in_recap(RECAP_CLEAN) == 0


def test_changes_are_summed_across_hosts():
    assert changed_in_recap(RECAP_DIRTY) == 3


def test_no_recap_is_not_the_same_as_no_changes():
    """The run died before finishing. Reading that as 'changed nothing' would
    report a playbook as idempotent on the strength of it having crashed."""
    assert changed_in_recap("fatal: [web-1]: UNREACHABLE!") is None
    assert changed_in_recap("") is None


def test_only_the_last_recap_counts():
    """A playbook with several plays prints a recap per run; the second pass's
    output is what is being judged."""
    assert changed_in_recap(RECAP_DIRTY + RECAP_CLEAN) == 0


def test_per_task_output_is_not_counted_as_a_change():
    """Under -vvvv, `changed=` appears in task result dicts too. Counting those
    would make every verbose run look non-idempotent."""
    noisy = ('ok: [web-1] => {"changed=1": "not a recap", "msg": "changed=9"}\n'
             'TASK [debug] ***\n' + RECAP_CLEAN)
    assert changed_in_recap(noisy) == 0
