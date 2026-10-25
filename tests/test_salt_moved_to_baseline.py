"""Salt left SLEP. Projects that already used it must fail loudly, not silently.

Configuration state — keeping hosts in a declared state over time — is Sysible
Baseline's job now. Baseline drives Salt directly and reacts to its event bus;
SLEP builds and provisions.

The risk in removing an engine is not the removal, it is the rows left behind.
An operator has projects with kind='salt', schedules that fire them nightly, and
pipeline steps that include them. Those rows are NOT deleted — they carry a
target, an inventory, a credential and a history somebody may still want to read
— so the engine lookup has to answer for a kind that no longer exists.

The failure mode being avoided: RUNNERS[kind] raising KeyError on a background
thread. Nothing catches it, the run sits in 'running' forever with an empty log,
and the nightly schedule just stops working with no sign of why.
"""
import time

import pytest

from backend import app as app_mod, db


def test_the_engine_is_gone_from_the_runner_table():
    assert "salt" not in app_mod.RUNNERS
    assert set(app_mod.RUNNERS) == {"ansible", "terraform", "molecule"}


def test_a_removed_engine_still_resolves_to_something_callable():
    """The whole point. A lookup that raises here raises on a daemon thread."""
    fn = app_mod._runner_for("salt")
    assert callable(fn)


def test_an_existing_salt_run_fails_with_a_reason(tmp_path, monkeypatch):
    """Not wedged in 'running' with an empty log."""
    monkeypatch.setattr(db, "RUNS_DIR", tmp_path)
    captured = {}
    monkeypatch.setattr(db, "set_run_status",
                        lambda rid, status, **kw: captured.update(id=rid, status=status, **kw))
    app_mod._runner_for("salt")(4242)
    assert captured["status"] == "failed", "a removed engine left the run unfinished"
    assert captured.get("exit_code") == 1
    log = (tmp_path / "4242.log").read_text()
    assert "Baseline" in log, "the log does not say where the capability went"
    assert "Salt" in log


def test_the_reason_names_the_product_that_took_it_over():
    why = app_mod.REMOVED_ENGINES["salt"]
    assert "Baseline" in why
    assert "recreate" in why.lower() or "moved" in why.lower(), \
        "the message does not tell the operator what to DO"


def test_an_unknown_engine_still_fails_rather_than_raising(tmp_path, monkeypatch):
    """A kind nobody has ever heard of — a typo, or a row from a future version
    restored into an older one — must take the same path."""
    monkeypatch.setattr(db, "RUNS_DIR", tmp_path)
    seen = {}
    monkeypatch.setattr(db, "set_run_status", lambda rid, status, **kw: seen.update(status=status))
    app_mod._runner_for("nonsense")(7)
    assert seen["status"] == "failed"
    assert "nonsense" in (tmp_path / "7.log").read_text()


def test_salt_is_not_offered_anywhere_new():
    """Removing the runner is not enough if the console still offers it: an
    operator would create a project they cannot run."""
    from backend import engines
    assert "salt" not in engines.ENGINES
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    ide = (root / "webgui" / "frontend" / "src" / "views" / "Ide.jsx").read_text()
    assert "value=\"salt\"" not in ide, "the IDE still offers Salt as an engine"
    sched = (root / "webgui" / "frontend" / "src" / "views" / "Schedules.jsx").read_text()
    assert "value=\"salt\"" not in sched, "schedules still offer Salt"
    assert not (root / "backend" / "runners" / "salt_runner.py").exists()
    assert not (root / "webgui" / "frontend" / "src" / "saltSnippets.js").exists()


def test_the_password_salt_was_not_caught_in_the_cull():
    """`salt` is also the per-admin password salt in this schema. A find-and-
    replace that removed Salt the engine and took the auth column with it would
    lock every operator out of the console — and the same trap bit the sibling
    repo during this change, where `rebaseline` became `reflashback` and killed
    enrollment. Pinned here before it can happen a second time."""
    import inspect
    src = inspect.getsource(db)
    assert "salt TEXT NOT NULL" in src, "the admin password-salt column is gone"
    assert "def add_admin(username, pw_hash, salt" in src
    assert "def set_admin_password(username, pw_hash, salt" in src
    app_src = inspect.getsource(app_mod)
    assert "def _pbkdf2(password: str, salt: str, iters: int)" in app_src
    from backend import infra
    assert "mksalt" in inspect.getsource(infra), "crypt.mksalt was removed from infra"
