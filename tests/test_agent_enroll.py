"""Agent (pull) enrollment: SLEP downloads a one-time bundle from the Controller with
its machine API key and installs it on each VM over SSH, so the VM self-enrolls — the
fix for 'enrollment not working' (SSH-host registration needed a superuser token SLEP
never has). Unit-level: the bundle fetch and the SSH install-command builder."""
import backend.controller_import as ci
import backend.keydist as keydist


class _Resp:
    def __init__(self, status, content=b"", payload=None, text=""):
        self.status_code = status
        self.content = content
        self._p = payload
        self.text = text

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


def test_fetch_agent_bundle_ok(monkeypatch):
    calls = {}

    def fake_get(url, headers=None, verify=None, timeout=None):
        calls["url"] = url
        calls["key"] = (headers or {}).get("X-API-Key")
        return _Resp(200, content=b"PK\x03\x04zipbytes")

    monkeypatch.setattr(ci.requests, "get", fake_get)
    out = ci.fetch_agent_bundle("https://ctrl.example:9000", "SECRET-KEY")
    assert out == b"PK\x03\x04zipbytes"
    assert calls["url"].endswith("/remote/agent-bundle")
    assert calls["key"] == "SECRET-KEY"          # authenticates with the machine API key


def test_fetch_agent_bundle_error_raises(monkeypatch):
    monkeypatch.setattr(ci.requests, "get",
                        lambda *a, **k: _Resp(409, payload={"detail": "no controller address"}))
    try:
        ci.fetch_agent_bundle("https://ctrl.example", "k")
        assert False, "should have raised"
    except ci.ControllerImportError as e:
        assert "no controller address" in str(e)


def test_agent_install_cmd_builds_ssh_argv():
    cmd = keydist.agent_install_cmd("root@jump", "admin@10.0.0.5", "/data/mk")
    assert cmd[0] == "ssh"
    assert "admin@10.0.0.5" in cmd and "/data/mk" in cmd
    assert any(a.startswith("-o") for a in cmd)
    assert any("ProxyCommand=" in a for a in cmd)      # jump hop
    assert "run_agent.sh" in cmd[-1] and "base64 -d" in cmd[-1]


def test_agent_install_cmd_no_bastion_and_no_key():
    direct = keydist.agent_install_cmd("", "admin@10.0.0.5", "/data/mk")
    assert not any("ProxyCommand" in a for a in direct)
    assert keydist.agent_install_cmd("", "admin@10.0.0.5", "") is None   # no key → no command
