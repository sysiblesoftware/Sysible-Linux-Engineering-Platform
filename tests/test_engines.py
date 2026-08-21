"""One-click engine install: status shape, guards, and install-log headers.
(The actual install downloads/pip-installs and needs network, so it isn't run
here — we exercise the plumbing and the endpoints.)"""
import backend.engines as engines


def test_status_lists_all_three_engines(client):
    d = client.get("/engines").json()["engines"]
    assert set(d) == {"ansible", "terraform", "salt"}
    for e in d.values():
        assert set(e) >= {"label", "binary", "installed", "installing"}


def test_ensure_path_prepends_managed_dirs():
    import os
    engines.ensure_path()
    assert str(engines._venv_dir() / "bin") in os.environ["PATH"]
    assert str(engines._bin_dir()) in os.environ["PATH"]


def test_install_unknown_engine_404(client):
    assert client.post("/engines/rust/install").status_code == 404
    assert client.get("/engines/rust/install-log").status_code == 404


def test_install_log_reports_status_header(client):
    r = client.get("/engines/ansible/install-log")
    assert r.status_code == 200
    assert "X-Install-Status" in r.headers and "X-Log-Next" in r.headers


def test_install_starts_without_touching_the_network(client, monkeypatch):
    # Assert the endpoint contract without kicking a real download: stub the
    # background installer.
    called = {}
    monkeypatch.setattr(engines, "start_install", lambda e: called.setdefault("engine", e))
    r = client.post("/engines/terraform/install")
    assert r.status_code == 200
    assert r.json()["status"] in ("started", "already-installed")
    if r.json()["status"] == "started":
        assert called["engine"] == "terraform"
