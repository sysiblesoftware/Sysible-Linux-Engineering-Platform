"""The Controller that shares this host should already be there.

The intended flow is: build infrastructure in SLEP, enrol it, then watch it in
the Controller. Every piece of that was already built -- the SLOP gateway stamps
the session's identity, SLEP trades it for the Controller's API key with no
password (exchange_sso_for_key), and the enrol step fetches a one-time agent
bundle per host. The only thing missing was a row in `controllers`.

So the first thing anyone had to do was open the Controller's portal, find its
address and hand it back to SLEP -- on a machine where both apps are the same
host, behind the same gateway, sharing the same secret. Reported as "I have to
set up Controller's web portal every time before using SLEP" and "SLEP should
know what the Controller's IP is because they are all running on the same host".

What this pins:

  * it happens, with no password and no typed address;
  * it is NARROW -- a forged identity header, a non-superuser, or no gateway at
    all must not reach the key exchange, because the key it returns is the
    Controller's master key;
  * an EXTERNAL Controller connected first does not stop the local one being
    adopted, and adopting the local one does not interfere with external ones;
  * a deliberate disconnect sticks, instead of the next page load undoing it.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from backend import app as appmod  # noqa: E402


class _Req:
    """Just enough Request for the helpers: headers."""
    def __init__(self, **headers):
        self.headers = {k.replace("_", "-").lower(): v for k, v in headers.items()}


# ---- the address it derives -------------------------------------------------
@pytest.mark.parametrize("host,expected", [
    ("172.16.100.129",       "https://172.16.100.129:9000"),
    ("172.16.100.129:443",   "https://172.16.100.129:9000"),
    ("slop.lan",             "https://slop.lan:9000"),
    ("slop.lan:8443",        "https://slop.lan:9000"),
])
def test_it_uses_the_host_the_portal_was_reached_at(host, expected):
    """A seeded address is one `ip route get` from install time; the host the
    browser actually reached is current. Sysible Connect learned this already."""
    assert appmod._local_controller_url(_Req(host=host)) == expected


def test_it_falls_back_to_the_seeded_address(monkeypatch):
    """Before any browser has arrived there is nothing else to go on."""
    monkeypatch.setenv("SLEP_CONTROLLER_URL", "https://10.0.0.5:9000")
    assert appmod._local_controller_url(_Req(host="localhost")) == "https://10.0.0.5:9000"


def test_no_address_rather_than_a_wrong_one(monkeypatch):
    monkeypatch.delenv("SLEP_CONTROLLER_URL", raising=False)
    assert appmod._local_controller_url(_Req(host="127.0.0.1")) == ""


# ---- what counts as the same Controller ------------------------------------
@pytest.mark.parametrize("a,b,same", [
    ("https://10.0.0.5:9000", "https://10.0.0.5:9000/",  True),
    ("https://10.0.0.5:9000", "HTTPS://10.0.0.5:9000",   True),
    ("https://10.0.0.5:9000", "http://10.0.0.5:9000",    True),   # scheme typo
    ("https://10.0.0.5:9000", "https://10.0.0.6:9000",   False),
    ("https://10.0.0.5:9000", "",                        False),
])
def test_the_same_controller_is_recognised_whatever_the_spelling(a, b, same):
    assert appmod._same_controller(a, b) is same


# ---- the guards, which matter most -----------------------------------------
#
# These assert the PURE precheck, not the wrapper. The wrapper catches broadly
# -- it runs on a page load and must not turn "list the Controllers" into an
# error -- and an earlier version of these tests went through it. That made
# them useless in exactly the way that matters: delete the superuser check and
# the next line raises TypeError, the handler swallows it into the same quiet
# False a refusal produces, and the test still passes. Three mutations with the
# guards removed survived. The decision is a separate function now, where
# nothing is caught, so a removed guard changes the answer.
def test_a_request_with_no_gateway_identity_is_refused(monkeypatch):
    """A bare call to SLEP's port must never reach the key exchange."""
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: None)
    ok, why = appmod._autoconnect_precheck(_Req(host="10.0.0.5"))
    assert (ok, why) == (False, "no_identity")


@pytest.mark.parametrize("role", ["operator", "auditor", "viewer", "", "super user", "superuser2"])
def test_a_non_superuser_is_refused(monkeypatch, role):
    """The Controller issues its master key to a superuser only; this must not
    become the way around that."""
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "sam", "role": role})
    monkeypatch.setattr(appmod.db, "meta_get", lambda *a, **k: "")
    monkeypatch.setattr(appmod.db, "list_controllers", lambda *a, **k: [])
    ok, why = appmod._autoconnect_precheck(_Req(host="10.0.0.5", x_sysible_role=role))
    assert (ok, why) == (False, "not_superuser"), f"role {role!r} was allowed to take the master key"


@pytest.mark.parametrize("role", ["superuser", "SUPERUSER", " Superuser "])
def test_the_role_is_read_the_same_way_the_controller_reads_it(monkeypatch, role):
    """The Controller does .strip().lower() on this header before comparing, so
    refusing a spelling it would accept would just disagree with it."""
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "admin", "role": role})
    monkeypatch.setattr(appmod.db, "meta_get", lambda *a, **k: "")
    monkeypatch.setattr(appmod.db, "list_controllers", lambda *a, **k: [])
    ok, _ = appmod._autoconnect_precheck(_Req(host="10.0.0.5", x_sysible_role=role))
    assert ok is True, f"{role!r} is a superuser to the Controller but not here"


def test_a_superuser_through_the_gateway_is_allowed(monkeypatch):
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "admin", "role": "superuser"})
    monkeypatch.setattr(appmod.db, "meta_get", lambda *a, **k: "")
    monkeypatch.setattr(appmod.db, "list_controllers", lambda *a, **k: [])
    ok, url = appmod._autoconnect_precheck(_Req(host="10.0.0.5", x_sysible_role="superuser"))
    assert ok is True and url == "https://10.0.0.5:9000"


def test_an_unreachable_controller_is_not_an_error(monkeypatch):
    """This runs on an ordinary page load."""
    monkeypatch.setattr(appmod, "_autoconnect_precheck", lambda r: (True, "https://10.0.0.5:9000"))
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "admin", "role": "superuser"})

    def boom(*a, **k):
        raise appmod.controller_import.ControllerImportError("connection refused")
    monkeypatch.setattr(appmod.controller_import, "exchange_sso_for_key", boom)
    assert appmod._autoconnect_local_controller(_Req(host="10.0.0.5")) is False


def test_an_external_controller_does_not_block_the_local_one(monkeypatch):
    """Connecting an external Controller first is normal and must not be what
    stops this host's own Controller being adopted."""
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "admin", "role": "superuser"})
    monkeypatch.setattr(appmod.db, "meta_get", lambda *a, **k: "")
    monkeypatch.setattr(appmod.db, "list_controllers",
                        lambda *a, **k: [{"base_url": "https://elsewhere.example:9000"}])
    ok, url = appmod._autoconnect_precheck(_Req(host="10.0.0.5", x_sysible_role="superuser"))
    assert ok is True and url == "https://10.0.0.5:9000"


def test_it_does_not_add_the_local_controller_twice(monkeypatch):
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "admin", "role": "superuser"})
    monkeypatch.setattr(appmod.db, "meta_get", lambda *a, **k: "")
    monkeypatch.setattr(appmod.db, "list_controllers",
                        lambda *a, **k: [{"base_url": "https://10.0.0.5:9000/"}])
    ok, why = appmod._autoconnect_precheck(_Req(host="10.0.0.5", x_sysible_role="superuser"))
    assert (ok, why) == (False, "already_registered")


def test_a_deliberate_disconnect_is_not_undone(monkeypatch):
    """Otherwise the next page load puts it back and the button looks broken."""
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "admin", "role": "superuser"})
    monkeypatch.setattr(appmod.db, "meta_get",
                        lambda k, d="": "1" if k == appmod._AUTOCONNECT_OPT_OUT else d)
    monkeypatch.setattr(appmod.db, "list_controllers", lambda *a, **k: [])
    ok, why = appmod._autoconnect_precheck(_Req(host="10.0.0.5", x_sysible_role="superuser"))
    assert (ok, why) == (False, "opted_out")


def test_a_bad_key_is_never_written_down(monkeypatch):
    """test_connection is the fail-closed check; a key that does not work must
    not become a saved Controller."""
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "admin", "role": "superuser"})
    monkeypatch.setattr(appmod.db, "meta_get", lambda *a, **k: "")
    monkeypatch.setattr(appmod.db, "list_controllers", lambda *a, **k: [])
    monkeypatch.setattr(appmod.controller_import, "exchange_sso_for_key", lambda *a, **k: "KEY")

    def refuse(*a, **k):
        raise appmod.controller_import.ControllerImportError("401 Unauthorized")
    monkeypatch.setattr(appmod.controller_import, "test_connection", refuse)
    made = []
    monkeypatch.setattr(appmod.db, "create_controller", lambda *a, **k: made.append(1))
    req = _Req(host="10.0.0.5", x_sysible_role="superuser")
    assert appmod._autoconnect_local_controller(req) is False
    assert not made, "a Controller was saved with a key that does not work"


def test_no_identity_is_refused_even_with_a_superuser_header(monkeypatch):
    """The header alone proves nothing -- only _gateway_identity, which checks
    the shared secret in constant time, does. This is the case that exposed a
    test matching on prose: with the identity guard deleted, the ROLE guard
    answered instead and its sentence happened to contain the word 'identity',
    so the test passed against a version with the guard gone."""
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: None)
    monkeypatch.setattr(appmod.db, "meta_get", lambda *a, **k: "")
    monkeypatch.setattr(appmod.db, "list_controllers", lambda *a, **k: [])
    ok, why = appmod._autoconnect_precheck(
        _Req(host="10.0.0.5", x_sysible_role="superuser", x_sysible_user="admin"))
    assert (ok, why) == (False, "no_identity"), (
        "a forged X-Sysible-Role header reached past the identity check")


def test_no_derivable_address_is_refused(monkeypatch):
    """Without this the precheck answers (True, "") and the connect is aimed at
    nothing. _same_controller is False for an empty URL, so the
    already-registered check does not catch it either."""
    monkeypatch.delenv("SLEP_CONTROLLER_URL", raising=False)
    monkeypatch.setattr(appmod, "_gateway_identity", lambda r: {"user": "admin", "role": "superuser"})
    monkeypatch.setattr(appmod.db, "meta_get", lambda *a, **k: "")
    monkeypatch.setattr(appmod.db, "list_controllers", lambda *a, **k: [])
    ok, why = appmod._autoconnect_precheck(_Req(host="localhost", x_sysible_role="superuser"))
    assert (ok, why) == (False, "no_address")
