"""Surveys — the form a job template shows the person launching it.

This is the piece that turns SLEP from "engineers run IaC" into "engineers
publish an automation and other people run it". A template without a survey is
a saved launch config; a template WITH one is a self-service form that someone
who cannot write Ansible can use safely, because every answer is typed, bounded,
and checked before anything runs.

Pure: a spec in, answers in, extra-vars out. No database, no Ansible, no HTTP —
which is the only reason the rules below can be tested exhaustively.

Two of those rules are security, not ergonomics:

  * A survey variable may not be one of Ansible's own connection/become
    variables. A field named `ansible_become_password` would let whoever
    launches the template set the sudo password the play runs with — handing a
    launcher a credential the template author never granted them. The whole
    point of a template is that the author decides what the launcher may change.
  * A `password` answer is returned SEPARATELY from the rest, so the caller can
    pass it to the run without writing it to the run row. Survey answers are
    persisted on the run (that is how a re-run works); a password among them
    would be a secret stored in a table any viewer can read.
"""
from __future__ import annotations

import re

# The field kinds a survey can ask for, mirroring AAP's set.
KINDS = ("text", "textarea", "password", "integer", "float", "choice", "multiselect", "boolean")
# Kinds whose answer must come from the field's own `choices`.
CHOICE_KINDS = ("choice", "multiselect")

# A valid Ansible variable name. Anything else is silently ignored by Ansible at
# run time, so the survey would collect an answer that goes nowhere.
_VAR_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Variables a survey must never set. `ansible_*` covers the connection and become
# family (ansible_become_password, ansible_user, ansible_host, ansible_port,
# ansible_ssh_private_key_file …): every one of them redirects WHERE the play
# runs or WHAT it runs as, which is the template author's decision, not the
# launcher's. `vault` is SLEP's own injected secret namespace.
_RESERVED_PREFIXES = ("ansible_",)
_RESERVED_EXACT = {"vault"}

MAX_FIELDS = 64
MAX_CHOICES = 128


class SurveyError(ValueError):
    """A spec that cannot be saved, or answers that cannot be launched."""


# --------------------------------------------------------------------------
# the spec, as the template author wrote it
# --------------------------------------------------------------------------
def validate_spec(spec) -> list[dict]:
    """Check a survey definition and return it normalised.

    Raises SurveyError with a message naming the field, because the author is a
    person looking at a form builder, not a log.
    """
    if spec in (None, "", []):
        return []
    if not isinstance(spec, list):
        raise SurveyError("a survey is a list of fields")
    if len(spec) > MAX_FIELDS:
        raise SurveyError(f"a survey may have at most {MAX_FIELDS} fields")

    out: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(spec, 1):
        if not isinstance(raw, dict):
            raise SurveyError(f"field {i}: each field is an object")
        var = str(raw.get("var") or "").strip()
        where = f"field {i}" + (f" ({var})" if var else "")
        if not var:
            raise SurveyError(f"{where}: needs a variable name")
        if not _VAR_RE.match(var):
            raise SurveyError(
                f"{where}: '{var}' is not a usable Ansible variable name — letters, "
                f"digits and underscores, not starting with a digit. Ansible would "
                f"ignore it, so the answer would go nowhere.")
        if var in _RESERVED_EXACT or var.startswith(_RESERVED_PREFIXES):
            raise SurveyError(
                f"{where}: '{var}' is reserved. A survey must not set Ansible's own "
                f"connection or become variables — that would let whoever launches "
                f"this template change who the play runs as.")
        if var in seen:
            raise SurveyError(f"{where}: '{var}' is asked for twice")
        seen.add(var)

        kind = str(raw.get("kind") or "text").strip()
        if kind not in KINDS:
            raise SurveyError(f"{where}: unknown field kind '{kind}'")

        field = {
            "var": var,
            "kind": kind,
            "label": str(raw.get("label") or var).strip(),
            "help": str(raw.get("help") or "").strip(),
            "required": bool(raw.get("required")),
        }

        if kind in CHOICE_KINDS:
            choices = raw.get("choices") or []
            if isinstance(choices, str):
                choices = [c.strip() for c in choices.splitlines() if c.strip()]
            if not isinstance(choices, list) or not choices:
                raise SurveyError(f"{where}: a {kind} field needs choices")
            if len(choices) > MAX_CHOICES:
                raise SurveyError(f"{where}: at most {MAX_CHOICES} choices")
            choices = [str(c) for c in choices]
            if len(set(choices)) != len(choices):
                raise SurveyError(f"{where}: the choices repeat")
            field["choices"] = choices

        if kind in ("integer", "float"):
            for bound in ("min", "max"):
                if raw.get(bound) not in (None, ""):
                    try:
                        field[bound] = int(raw[bound]) if kind == "integer" else float(raw[bound])
                    except (TypeError, ValueError):
                        raise SurveyError(f"{where}: {bound} must be a number")
            if "min" in field and "max" in field and field["min"] > field["max"]:
                raise SurveyError(f"{where}: min is greater than max")

        # A default must itself be a valid answer, or the form opens already
        # broken and the person filling it in gets the blame.
        if raw.get("default") not in (None, ""):
            try:
                field["default"] = _coerce(field, raw["default"])
            except SurveyError as e:
                raise SurveyError(f"{where}: the default is not valid — {e}")
        # A required password with a stored default would mean the secret lives
        # in the template row, which is exactly what the vault is for.
        if kind == "password" and "default" in field:
            raise SurveyError(f"{where}: a password field cannot have a default — "
                              f"reference a vault secret in the playbook instead")

        out.append(field)
    return out


def has_password(spec) -> bool:
    return any(f.get("kind") == "password" for f in (spec or []))


# --------------------------------------------------------------------------
# the answers, as the person launching it gave them
# --------------------------------------------------------------------------
def validate_answers(spec, answers) -> tuple[dict, dict]:
    """(extra_vars, secret_vars) for a launch.

    The split is the point: `extra_vars` is persisted on the run row so a re-run
    can repeat it, and `secret_vars` holds the password answers, which the caller
    passes to the runner transiently and never stores.
    """
    spec = spec or []
    answers = answers or {}
    if not isinstance(answers, dict):
        raise SurveyError("answers must be an object of variable → value")

    extra: dict = {}
    secret: dict = {}
    for field in spec:
        var, kind = field["var"], field["kind"]
        given = answers.get(var)
        missing = given in (None, "") or (kind == "multiselect" and given == [])

        if missing:
            if "default" in field:
                value = field["default"]
            elif field.get("required"):
                raise SurveyError(f"{field.get('label') or var}: required")
            else:
                continue                      # unanswered and optional: say nothing
        else:
            value = _coerce(field, given)

        if kind == "password":
            secret[var] = value
        else:
            extra[var] = value

    # An answer to something the survey never asked for is a mistake worth
    # naming: it is usually a renamed field, and silently dropping it means the
    # playbook runs with the old value (or none) and nobody finds out.
    unknown = set(answers) - {f["var"] for f in spec}
    if unknown:
        raise SurveyError("this survey does not ask for: " + ", ".join(sorted(unknown)))
    return extra, secret


def _coerce(field, value):
    kind = field["kind"]
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "yes", "on", "1"):
            return True
        if s in ("false", "no", "off", "0"):
            return False
        raise SurveyError("expected true or false")

    if kind in ("integer", "float"):
        try:
            n = int(value) if kind == "integer" else float(value)
        except (TypeError, ValueError):
            raise SurveyError(f"expected {'a whole number' if kind == 'integer' else 'a number'}")
        if "min" in field and n < field["min"]:
            raise SurveyError(f"must be at least {field['min']}")
        if "max" in field and n > field["max"]:
            raise SurveyError(f"must be at most {field['max']}")
        return n

    if kind == "choice":
        s = str(value)
        if s not in field["choices"]:
            raise SurveyError(f"must be one of: {', '.join(field['choices'])}")
        return s

    if kind == "multiselect":
        vals = value if isinstance(value, list) else [value]
        out = []
        for v in vals:
            s = str(v)
            if s not in field["choices"]:
                raise SurveyError(f"'{s}' is not one of: {', '.join(field['choices'])}")
            if s not in out:
                out.append(s)
        return out

    return str(value)
