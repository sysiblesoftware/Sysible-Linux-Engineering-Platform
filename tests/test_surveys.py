"""Surveys: the form a job template shows the person launching it.

SLEP is built for engineers creating infrastructure. The thing AAP actually
sells on top of Ansible is the step after that: an engineer publishes an
automation, and someone who cannot write a playbook runs it from a form. A
survey is that form, and it only works if every answer is typed, bounded and
checked BEFORE anything runs — otherwise it is a text box wired to a shell.

Two of these tests are about privilege rather than ergonomics, and they are the
reason this module exists as its own validated layer:

  * a survey variable may not be one of Ansible's own connection/become
    variables — a field named `ansible_become_password` would let whoever
    launches the template choose the sudo password the play runs with, which is
    a credential the author never granted them;
  * password answers come back separately from everything else, because survey
    answers are persisted on the run row (that is how a re-run repeats them) and
    a secret among them would be readable by anyone who can list runs.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import surveys  # noqa: E402
from backend.surveys import SurveyError  # noqa: E402


def spec(*fields):
    return surveys.validate_spec(list(fields))


def bad_spec(*fields):
    with pytest.raises(SurveyError) as e:
        surveys.validate_spec(list(fields))
    return str(e.value)


def answer(s, **given):
    return surveys.validate_answers(s, given)


def refused(s, **given):
    with pytest.raises(SurveyError) as e:
        surveys.validate_answers(s, given)
    return str(e.value)


# ---- the two that are about privilege ---------------------------------------
@pytest.mark.parametrize("var", [
    "ansible_become_password", "ansible_user", "ansible_host", "ansible_port",
    "ansible_ssh_private_key_file", "ansible_connection",
])
def test_a_survey_cannot_ask_for_ansibles_own_variables(var):
    """Every one of these redirects WHERE the play runs or WHAT it runs as. The
    template author decides that; the launcher does not get to."""
    msg = bad_spec({"var": var, "kind": "text"})
    assert "reserved" in msg
    assert "runs as" in msg


def test_a_survey_cannot_shadow_the_secret_namespace():
    """SLEP injects its vault as `vault.<name>`. A survey field called `vault`
    would replace every secret the playbook reads with whatever was typed."""
    assert "reserved" in bad_spec({"var": "vault", "kind": "text"})


def test_a_password_answer_does_not_go_into_the_persisted_vars():
    """Run rows keep their extra_vars so a re-run can repeat them. A survey
    password in there is a secret in a table any viewer can read."""
    s = spec({"var": "app_version", "kind": "text"},
             {"var": "db_password", "kind": "password", "required": True})
    extra, secret = answer(s, app_version="1.4.2", db_password="hunter2")
    assert extra == {"app_version": "1.4.2"}
    assert secret == {"db_password": "hunter2"}
    assert "hunter2" not in str(extra)


def test_a_password_field_cannot_carry_a_stored_default():
    """That is a secret living in the template row. The vault is for this."""
    msg = bad_spec({"var": "db_password", "kind": "password", "default": "hunter2"})
    assert "cannot have a default" in msg and "vault" in msg


# ---- a field has to actually reach the playbook -----------------------------
@pytest.mark.parametrize("var", ["2fast", "has-dash", "has space", "", "a.b"])
def test_a_variable_name_ansible_would_ignore_is_refused(var):
    """Ansible drops a name like this, so the survey would collect an answer
    that goes nowhere and the playbook would run with its default — looking like
    the form did nothing."""
    msg = bad_spec({"var": var, "kind": "text"})
    assert "variable name" in msg or "needs a variable name" in msg


def test_the_same_variable_cannot_be_asked_for_twice():
    assert "twice" in bad_spec({"var": "env", "kind": "text"},
                               {"var": "env", "kind": "text"})


def test_an_answer_to_a_field_that_does_not_exist_is_refused():
    """Usually a renamed field. Dropping it silently means the playbook runs
    with the old value and nobody finds out."""
    s = spec({"var": "env", "kind": "text"})
    assert "does not ask for: oldname" in refused(s, env="prod", oldname="x")


# ---- typed answers, which is the whole point --------------------------------
def test_a_choice_must_come_from_the_choices():
    """Otherwise it is a text box with extra steps."""
    s = spec({"var": "env", "kind": "choice", "choices": ["dev", "prod"]})
    assert answer(s, env="prod")[0] == {"env": "prod"}
    assert "must be one of: dev, prod" in refused(s, env="staging")


def test_a_choice_field_with_no_choices_is_refused():
    assert "needs choices" in bad_spec({"var": "env", "kind": "choice"})


def test_multiselect_keeps_the_order_and_drops_duplicates():
    s = spec({"var": "roles", "kind": "multiselect", "choices": ["web", "db", "cache"]})
    assert answer(s, roles=["db", "web", "db"])[0] == {"roles": ["db", "web"]}
    assert "not one of" in refused(s, roles=["web", "nope"])


@pytest.mark.parametrize("given,expect", [("7", 7), (7, 7), (" 7 ", 7)])
def test_an_integer_arrives_as_an_integer(given, expect):
    """A playbook that does `when: count > 3` on the string "7" compares wrong."""
    s = spec({"var": "count", "kind": "integer"})
    assert answer(s, count=given)[0] == {"count": expect}


def test_a_number_outside_its_bounds_is_refused():
    s = spec({"var": "count", "kind": "integer", "min": 1, "max": 10})
    assert answer(s, count=10)[0] == {"count": 10}
    assert "at most 10" in refused(s, count=11)
    assert "at least 1" in refused(s, count=0)


def test_bounds_that_cannot_both_be_met_are_refused():
    assert "min is greater than max" in bad_spec(
        {"var": "n", "kind": "integer", "min": 10, "max": 1})


@pytest.mark.parametrize("given,expect", [
    ("yes", True), ("true", True), ("1", True), (True, True),
    ("no", False), ("false", False), ("0", False), (False, False),
])
def test_a_boolean_arrives_as_a_boolean(given, expect):
    """`when: do_restart` on the string "false" is TRUE — a non-empty string."""
    s = spec({"var": "do_restart", "kind": "boolean"})
    assert answer(s, do_restart=given)[0] == {"do_restart": expect}


def test_a_boolean_that_is_neither_is_refused():
    s = spec({"var": "do_restart", "kind": "boolean"})
    assert "true or false" in refused(s, do_restart="maybe")


# ---- required, defaults, and saying nothing ---------------------------------
def test_a_required_field_with_no_answer_stops_the_launch():
    s = spec({"var": "env", "kind": "text", "required": True, "label": "Environment"})
    assert "Environment: required" in refused(s)


def test_a_default_fills_in_for_an_unanswered_field():
    s = spec({"var": "env", "kind": "text", "default": "dev"})
    assert answer(s)[0] == {"env": "dev"}
    assert answer(s, env="prod")[0] == {"env": "prod"}


def test_an_unanswered_optional_field_sets_nothing_at_all():
    """Not an empty string. Setting `env: ""` overrides whatever default the
    playbook or inventory had, which is not what leaving a box blank means."""
    s = spec({"var": "env", "kind": "text"})
    assert answer(s)[0] == {}


def test_a_default_that_is_not_a_valid_answer_is_refused_at_save_time():
    """Otherwise the form opens already broken and the person filling it in gets
    the blame for a mistake the author made."""
    msg = bad_spec({"var": "env", "kind": "choice", "choices": ["dev"], "default": "prod"})
    assert "the default is not valid" in msg
    assert "must be one of" in msg


def test_a_required_field_with_a_default_never_blocks_a_launch():
    s = spec({"var": "env", "kind": "text", "required": True, "default": "dev"})
    assert answer(s)[0] == {"env": "dev"}


# ---- the shape of a survey itself -------------------------------------------
def test_no_survey_at_all_is_fine():
    for empty in (None, "", []):
        assert surveys.validate_spec(empty) == []
    assert surveys.validate_answers([], {}) == ({}, {})


def test_an_unknown_field_kind_is_refused():
    assert "unknown field kind" in bad_spec({"var": "x", "kind": "slider"})


def test_a_field_falls_back_to_its_variable_name_for_a_label():
    assert spec({"var": "app_version", "kind": "text"})[0]["label"] == "app_version"


def test_choices_may_be_written_as_lines():
    """A form builder gives you a textarea, not a JSON array."""
    s = spec({"var": "env", "kind": "choice", "choices": "dev\nprod\n\n"})
    assert s[0]["choices"] == ["dev", "prod"]


def test_repeated_choices_are_refused():
    assert "choices repeat" in bad_spec(
        {"var": "env", "kind": "choice", "choices": ["dev", "dev"]})


def test_a_survey_cannot_be_unbounded():
    many = [{"var": f"v{i}", "kind": "text"} for i in range(surveys.MAX_FIELDS + 1)]
    with pytest.raises(SurveyError) as e:
        surveys.validate_spec(many)
    assert "at most" in str(e.value)


def test_it_tells_the_author_which_field_is_wrong():
    """The author is looking at a form builder, not a log."""
    msg = bad_spec({"var": "ok", "kind": "text"}, {"var": "env", "kind": "slider"})
    assert "field 2" in msg and "env" in msg


def test_has_password_reports_whether_the_launch_needs_one():
    assert surveys.has_password(spec({"var": "p", "kind": "password"})) is True
    assert surveys.has_password(spec({"var": "e", "kind": "text"})) is False
