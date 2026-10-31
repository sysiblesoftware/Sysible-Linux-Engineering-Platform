"""An expired SLOP session must not be parsed as an API response.

SLOP's gateway answers an unauthenticated request — XHR included — with
`redir * /login?next={uri} 302` (gateway/Caddyfile), fetch FOLLOWS it, and the
IdP serves its sign-in page as 200 text/html. So "signed out at SLOP" arrives at
the console as a successful 200, not a 401.

api() only treated 401 as unauthenticated, and returned a non-JSON body as a
plain string with r.ok true. App.jsx assigned that string to `me`, so
me.username and me.role were undefined and it still called setAuthed(true): the
console rendered with its placeholders — "admin" / "operator" — for a session
that no longer existed. Every view that reads a key off its own response then
crashed the React root to a white page (Controllers.jsx does
setControllers(undefined) then reads .length; Activity.jsx calls .filter), and
the run-log pane printed the sign-in page's raw HTML and kept polling it.

SLOP's SESSION_TTL is 12 hours, so this is what a tab left open overnight did.

Browser-side contract, no JS test runner in the repo, so it is pinned as source
— the way tests/test_console_api_contract.py and the other console tests do it.
"""
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
DIST = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "dist"


@pytest.fixture(scope="module")
def api_js():
    return (SRC / "api.js").read_text()


def test_both_signals_are_checked(api_js):
    assert "r.redirected" in api_js, (
        "fetch exposes .redirected when it followed the gateway's 302")
    assert "ct.includes('html')" in api_js, (
        "the sign-in page arrives as 200 text/html; every /api/* path this "
        "client calls answers JSON or plain text")


def test_the_check_runs_on_a_200(api_js):
    """Behind `if (!r.ok)` is exactly where the bug was."""
    i_guard = api_js.index("_isSignInPage(r, ct)")
    i_notok = api_js.index("if (!r.ok)")
    assert i_guard < i_notok


def test_it_applies_to_the_log_tail_too(api_js):
    """tail() had the same hole and a worse symptom: the run-log pane rendered
    the sign-in page's HTML and polled it for ever."""
    tail = api_js[api_js.index("export async function tail"):]
    assert "_isSignInPage(r, ct)" in tail, tail


def test_it_bounces_to_the_platform_sign_in(api_js):
    """Clearing the token is not enough: SLEP has no login of its own under
    SSO, so only a top-level navigation reaches the platform sign-in."""
    fn = api_js[api_js.index("function _sessionGone"):]
    fn = fn[:fn.index("\n}\n")]
    assert "slep-logout" in fn
    assert "window.location.reload()" in fn


def test_a_plain_401_does_not_reload(api_js):
    """A standalone SLEP's own expired bearer is a 401 and belongs on the normal
    sign-in screen — reloading there would just bounce the operator."""
    assert "r.status === 401) throw _sessionGone({ reload: false })" in api_js


@pytest.mark.parametrize("view,fn", [("views/Inventories.jsx", "tail("),
                                     ("views/Runs.jsx", "tail(")])
def test_a_polling_view_stops_when_the_tail_throws(view, fn):
    """tail() now throws, and these loops run on an interval. Without a catch
    the interval keeps firing and rejecting until the page reloads."""
    src = (SRC / view).read_text()
    for i, line in enumerate(src.splitlines()):
        if "await " + fn in line:
            window = "\n".join(src.splitlines()[max(0, i - 4):i + 8])
            assert "try {" in window and "catch" in window, f"{view}:{i + 1}\n{window}"


def test_the_built_console_carries_the_guard():
    if not DIST.is_dir():
        pytest.skip("webgui/frontend/dist not built in this checkout")
    blobs = list(DIST.rglob("*.js"))
    assert blobs, "no built JS found"
    assert any("Session expired" in b.read_text(errors="ignore") for b in blobs), \
        "rebuild the console (npm run build)"
