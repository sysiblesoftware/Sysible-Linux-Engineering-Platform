"""Notifications: a run that fails in the console and nowhere else.

SLEP's runs were visible in the console and that was it, so a nightly schedule
that started failing kept failing until a human happened to look. An automation
you publish for other people is only useful if its failures reach somebody.

Three rules carry the risk here, and each has a test that fails loudly:

  1. A notification must not break a run. Delivery is on a background thread with
     a bounded timeout, and db.set_run_status calls the hook AFTER the status is
     written — a chat server being down must not leave a finished run saying
     "running", and must not raise inside the runner's thread.
  2. The payload must not carry secrets. A run's extra_vars hold whatever someone
     typed into the Variables box, and a webhook URL is an address outside this
     platform.
  3. Only http(s). The URL is operator-supplied and SLEP posts to it from inside
     the network; file:// turns "notify me" into "read this host's files".
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.db as db  # noqa: E402
from backend import notifications as notif  # noqa: E402


# ---- configuration is checked when it is saved ------------------------------
@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://h/f",
                                 "javascript:alert(1)"])
def test_only_http_webhooks_are_accepted(url):
    """SLEP posts to this from inside your network."""
    with pytest.raises(notif.NotificationError) as e:
        notif.validate_config("webhook", {"url": url})
    assert "http or https" in str(e.value)


def test_a_webhook_needs_a_url_with_a_host():
    for bad in ({}, {"url": ""}, {"url": "https://"}):
        with pytest.raises(notif.NotificationError):
            notif.validate_config("webhook", bad)
    assert notif.validate_config("webhook", {"url": "https://hooks.example/x"})["url"]


def test_email_needs_a_host_and_a_real_recipient():
    with pytest.raises(notif.NotificationError):
        notif.validate_config("email", {"to": ["a@b.c"]})
    with pytest.raises(notif.NotificationError):
        notif.validate_config("email", {"host": "smtp", "to": []})
    with pytest.raises(notif.NotificationError) as e:
        notif.validate_config("email", {"host": "smtp", "to": ["not-an-address"]})
    assert "not an email address" in str(e.value)


def test_recipients_may_be_written_the_way_people_type_them():
    cfg = notif.validate_config("email", {"host": "smtp", "to": "a@x.com, b@x.com;c@x.com"})
    assert cfg["to"] == ["a@x.com", "b@x.com", "c@x.com"]


def test_an_impossible_smtp_port_is_refused():
    with pytest.raises(notif.NotificationError):
        notif.validate_config("email", {"host": "s", "to": ["a@b.c"], "port": 70000})


# ---- which outcomes fire ----------------------------------------------------
@pytest.mark.parametrize("status,on_s,on_f,expect", [
    ("success", True, False, True), ("success", False, True, False),
    ("failed", False, True, True), ("error", False, True, True),
    ("canceled", False, True, True), ("failed", True, False, False),
    ("running", True, True, False),
])
def test_a_rule_fires_only_for_the_outcomes_it_asked_for(status, on_s, on_f, expect):
    assert notif.wants({"on_success": on_s, "on_failure": on_f}, status) is expect


# ---- the payload ------------------------------------------------------------
def test_the_payload_never_carries_the_runs_variables():
    """extra_vars is whatever an operator typed into the Variables box. This
    leaves the platform."""
    run = {"id": 7, "status": "failed", "kind": "ansible", "target": "site.yml",
           "exit_code": 2, "extra_vars": {"db_password": "hunter2"}}
    body = notif.payload(run, {"name": "Prod"}, {"name": "Patch"})
    assert "hunter2" not in json.dumps(body)
    assert "extra_vars" not in body


def test_the_payload_says_what_happened():
    run = {"id": 7, "status": "failed", "kind": "ansible", "target": "site.yml",
           "exit_code": 2}
    body = notif.payload(run, {"name": "Prod"}, {"name": "Patch the web tier"})
    assert body["ok"] is False and body["status"] == "failed"
    assert body["run_id"] == 7 and body["exit_code"] == 2
    assert body["project"] == "Prod" and body["template"] == "Patch the web tier"
    assert "Patch the web tier" in body["text"]


def test_a_link_is_included_when_slep_knows_its_own_address():
    body = notif.payload({"id": 7, "status": "success"}, base_url="https://slep.lan/")
    assert body["url"] == "https://slep.lan/#/runs/7"
    assert "url" not in notif.payload({"id": 7, "status": "success"})


# ---- delivery, against a real listener --------------------------------------
class _Hook(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Hook.received.append({"body": json.loads(self.rfile.read(n) or b"{}"),
                               "sig": self.headers.get("X-Sysible-Signature")})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def hook():
    _Hook.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook"
    srv.shutdown()


def test_a_webhook_actually_arrives(hook):
    ok, detail = notif.deliver({"kind": "webhook", "config": {"url": hook}},
                               {"event": "run_finished", "status": "failed"})
    assert ok, detail
    assert _Hook.received[0]["body"]["status"] == "failed"


def test_a_signed_webhook_can_be_verified_by_the_receiver(hook):
    """An incoming webhook URL is the only thing protecting most receivers.
    The signature is how they tell a real notification from anyone who learned
    the URL."""
    import hashlib
    import hmac
    body = {"event": "run_finished", "status": "success"}
    ok, _ = notif.deliver({"kind": "webhook",
                           "config": {"url": hook, "secret": "s3cret"}}, body)
    assert ok
    got = _Hook.received[0]
    expect = "sha256=" + hmac.new(b"s3cret", json.dumps(body).encode(),
                                  hashlib.sha256).hexdigest()
    assert got["sig"] == expect


def test_an_unsigned_webhook_sends_no_signature_header(hook):
    notif.deliver({"kind": "webhook", "config": {"url": hook}}, {"status": "success"})
    assert _Hook.received[0]["sig"] is None


def test_a_bad_scheme_is_refused_at_send_time_too():
    """Re-checked when sending, not only when saving: a stored row can be edited
    by anything that can write the database."""
    ok, detail = notif.deliver({"kind": "webhook", "config": {"url": "file:///etc/passwd"}},
                               {"status": "failed"})
    assert not ok and "non-http(s)" in detail


def test_a_dead_receiver_is_a_reported_failure_not_an_exception():
    ok, detail = notif.deliver(
        {"kind": "webhook", "config": {"url": "http://127.0.0.1:1/nothing"}},
        {"status": "failed"})
    assert ok is False and detail


def test_an_unknown_kind_does_not_raise():
    ok, detail = notif.deliver({"kind": "carrier-pigeon", "config": {}}, {})
    assert ok is False and "unknown" in detail


# ---- it must not be able to break a run -------------------------------------
def _project(prefix):
    """A real project row, since a run needs one to point at. The `client`
    fixture is what brings the schema up (it triggers the app's startup)."""
    name = f"{prefix}-{int(time.time() * 1000) % 1000000}"
    created = db.create_project(name, name.lower())
    return created["id"] if isinstance(created, dict) else created



def test_a_notifier_that_raises_cannot_fail_the_run(client):
    """db.set_run_status calls the hook AFTER writing the status, and swallows
    anything it throws. Otherwise an unreachable chat server leaves a finished
    run recorded as still running."""
    called = {"n": 0}

    def boom(run_id, status):
        called["n"] += 1
        raise RuntimeError("the chat server is on fire")

    old = db._on_run_finished
    db.set_run_finished_hook(boom)
    try:
        pid = _project("notif-raise")
        rid = db.create_run(pid, "ansible", "site.yml", created_by="t")
        db.set_run_status(rid, "failed", exit_code=2)
        assert called["n"] == 1, "the hook never fired"
        assert db.get_run(rid)["status"] == "failed", \
            "the run's own status was lost because a notifier raised"
    finally:
        db.set_run_finished_hook(old)


def test_the_hook_only_fires_on_a_finished_run(client):
    seen = []
    old = db._on_run_finished
    db.set_run_finished_hook(lambda rid, st: seen.append(st))
    try:
        pid = _project("notif-finish")
        rid = db.create_run(pid, "ansible", "site.yml", created_by="t")
        db.set_run_status(rid, "running", started=int(time.time()))
        assert seen == [], "a run that merely started was announced as finished"
        db.set_run_status(rid, "success", exit_code=0)
        assert seen == ["success"]
    finally:
        db.set_run_finished_hook(old)


def test_delivery_happens_off_the_callers_thread(hook):
    """A run is finished the moment its status is written. A slow receiver must
    not hold the runner's thread."""
    t = notif.send_async([{"kind": "webhook", "config": {"url": hook}}],
                         {"status": "success"})
    assert isinstance(t, threading.Thread)
    t.join(timeout=10)
    assert _Hook.received, "nothing was delivered"
