"""A run log is readable by any authenticated viewer, including an auditor.

Each runner injects values the viewer is not entitled to see, and each runner was
masking exactly one of them — extra_vars. The credential was masked nowhere:

  * terraform puts a cloud credential's KEY=VALUE pairs in the child's
    environment, and `terraform plan` prints resource attributes
  * ansible writes an ssh_password into the inventory as a host var, and `-vvv`
    prints the inventory
  * salt writes the same password into the roster

And ansible masked its echoed command line but not a single line of the output
that followed it, which is where a tool actually prints what it was given.

These tests drive the real masking path with real subprocesses that echo a
secret, rather than asserting on the shape of the redact list.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.runners import _common

CLOUD = {
    "kind": "cloud", "name": "aws-lab",
    "secret": (
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        "# a comment, and a blank line follow\n"
        "\n"
        'export AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI-K7MDENG-bPxRfiCY"\n'
    ),
}
SSH_PW = {"kind": "ssh_password", "name": "root-pw", "secret": "c0rrect-horse-battery"}


# ---- what goes into the redact list ----------------------------------------
def test_a_cloud_credentials_values_are_redacted():
    vals = _common.secret_values(CLOUD, {"db_pass": "hunter2secret"})
    assert "wJalrXUtnFEMI-K7MDENG-bPxRfiCY" in vals, "the cloud secret is not masked"
    assert "AKIAIOSFODNN7EXAMPLE" in vals
    assert "hunter2secret" in vals, "extra_vars stopped being masked"


def test_the_names_are_not_redacted():
    """Masking the variable NAMES would make the log unreadable — "***=***"
    tells nobody anything — and they are not secret."""
    vals = _common.secret_values(CLOUD, None)
    assert not any(v.startswith("AWS_SECRET") for v in vals), vals


def test_an_ssh_password_is_redacted():
    assert _common.secret_values(SSH_PW, None) == ["c0rrect-horse-battery"]


def test_a_private_key_is_not_in_the_list():
    """Keys are written to a 0600 file and passed by path, so they are never
    in-band; a PEM is also line-wrapped, which this mechanism cannot match."""
    assert _common.secret_values({"kind": "ssh", "secret": "-----BEGIN-----\nabc\n"}, None) == []


def test_trivially_short_values_are_left_alone():
    """Masking "a" would turn every a in the log into ***."""
    assert _common.secret_values(None, {"x": "ab", "y": "1", "z": "abc"}) == ["abc"]


def test_no_credential_and_no_vars_is_empty():
    assert _common.secret_values(None, None) == []
    assert _common.secret_values({"kind": "cloud"}, {}) == []


# ---- the masking actually reaching a log -----------------------------------
SECRET = "wJalrXUtnFEMI-K7MDENG-bPxRfiCY"

# Reads the secret out of its ENVIRONMENT and prints it, which is precisely what
# a terraform provider does when it quotes a failing request. Not argv: that
# would put it in `ps` for any local user, which is the thing the runners go out
# of their way to avoid.
_ECHOER = "import os; print('provider error: secret=' + os.environ['AWS_SECRET_ACCESS_KEY'])"


def _run_echoer(tmp_path, redact):
    env = _common.credential_env(CLOUD, os.environ)
    log_path = tmp_path / "run.log"
    with open(log_path, "w") as log:
        rc = _common.stream([sys.executable, "-c", _ECHOER], tmp_path, env, log,
                            redact=redact)
    return rc, log_path.read_text()


def test_a_secret_echoed_by_the_child_is_masked_in_the_log(tmp_path):
    """The real path: a child prints the secret it was given, exactly as
    `terraform plan` or `ansible -vvv` would, and the log must not contain it."""
    rc, out = _run_echoer(tmp_path, _common.secret_values(CLOUD, None))
    assert rc == 0, out            # a child that crashed proves nothing
    assert "provider error:" in out, out
    assert SECRET not in out, "the cloud secret reached the run log"
    assert "***" in out, out


def test_without_the_credential_in_the_list_it_would_leak(tmp_path):
    """The inverse, so the test above cannot pass for the wrong reason: the same
    child, masked with extra_vars only, is exactly the old behaviour."""
    rc, out = _run_echoer(tmp_path, ["hunter2secret"])
    assert rc == 0, out
    assert SECRET in out, "this assertion documents the old leak"


# ---- the runners are wired to the shared helper ----------------------------
def _src(name):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "backend", "runners", name), encoding="utf-8") as fh:
        return fh.read()


def test_every_runner_uses_the_shared_helper():
    """Three hand-rolled lists is how one of them ends up without the credential,
    which is what had happened."""
    for name in ("terraform_runner.py", "salt_runner.py", "ansible_runner.py"):
        src = _src(name)
        assert "_common.secret_values(" in src, f"{name} builds its own redact list"
        assert "for v in extra_vars.values()" not in src, \
            f"{name} still has a hand-rolled redact list"


def test_terraform_redacts_init_as_well():
    """A backend-config failure quotes what it was given, and init was the one
    stream() call with no redact at all."""
    src = _src("terraform_runner.py")
    init_call = next(l for l in src.splitlines()
                     if "_common.stream(init" in l)
    assert "redact=redact" in init_call, init_call


def test_ansible_masks_its_output_not_only_its_command_line():
    """It masked the echoed command and then wrote every output line through
    untouched, which is the half that matters."""
    src = _src("ansible_runner.py")
    writes = [l.strip() for l in src.splitlines() if ".stdout:" in l]
    assert len(writes) == 2, writes
    assert src.count("log.write(_common.mask(line, redact) if redact else line)") == 2, \
        "an output loop still writes the raw line to the log"
