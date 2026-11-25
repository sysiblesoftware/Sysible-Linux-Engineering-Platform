"""A Controller URL with a mistyped scheme must not become a DNS error for "https".

Typed into the Connect box as `https//192.168.8.139` -- no colon -- the old
normaliser saw something that did not start with "http://" or "https://", took
it for a bare hostname, and prefixed a scheme:

    Could not reach the Controller at https://https//192.168.8.139/auth/api-key:
    HTTPSConnectionPool(host='https', port=443): Max retries exceeded with url:
    //192.168.8.139/auth/api-key (Caused by NameResolutionError(... Failed to
    resolve 'https' ([Errno -5] No address associated with hostname)))

The host it names is "https". Nothing in it mentions a missing colon, and the
operator is left looking at the network between two machines that are fine.

Reported as "Controller isn't connecting to SLEP even though they are on the
same IP", which is exactly what it looks like.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from backend.controller_import import ControllerImportError, _normalize_base  # noqa: E402


@pytest.mark.parametrize("typed,expected", [
    # The one from the report, and its neighbours. Each is a thing a hand
    # reaching for ":" and "/" actually produces.
    ("https//192.168.8.139",   "https://192.168.8.139"),   # no colon
    ("https:/192.168.8.139",   "https://192.168.8.139"),   # one slash
    ("https:192.168.8.139",    "https://192.168.8.139"),   # no slashes
    ("http//192.168.8.139",    "http://192.168.8.139"),
    ("HTTPS//192.168.8.139",   "https://192.168.8.139"),   # caps lock
    ("  https//192.168.8.139 ", "https://192.168.8.139"),  # pasted with spaces
])
def test_a_mistyped_scheme_is_repaired(typed, expected):
    assert _normalize_base(typed) == expected


@pytest.mark.parametrize("typed,expected", [
    ("https://192.168.8.139",       "https://192.168.8.139"),
    ("https://192.168.8.139/",      "https://192.168.8.139"),
    ("http://ctl.example.com:8080", "http://ctl.example.com:8080"),
    ("192.168.8.139",               "https://192.168.8.139"),   # bare host
    ("controller.example.com",      "https://controller.example.com"),
])
def test_urls_that_were_already_right_are_unchanged(typed, expected):
    assert _normalize_base(typed) == expected


def test_a_hostname_that_merely_starts_with_http_is_not_a_typo():
    """The repair must not eat a real hostname. "httpsfoo.example" is a host,
    not a mistyped scheme, and the rule has to tell them apart."""
    assert _normalize_base("httpsfoo.example") == "https://httpsfoo.example"
    assert _normalize_base("http-proxy.example") == "https://http-proxy.example"


@pytest.mark.parametrize("typed", ["https//", "https://", "http:", "://"])
def test_a_url_with_no_host_is_refused_here_and_says_so(typed):
    """Not repairable into anything. The complaint has to name the URL, where
    the operator can act on it, rather than surfacing later as a failure to
    resolve some fragment of what they typed."""
    with pytest.raises(ControllerImportError) as ei:
        _normalize_base(typed)
    msg = str(ei.value)
    assert "no host in it" in msg or "required" in msg, msg


def test_the_error_never_blames_a_host_called_https():
    """The specific confusion this removes: whatever happens, SLEP must not go
    on to look up a machine named after the scheme."""
    for typed in ("https//192.168.8.139", "https:/192.168.8.139", "https:192.168.8.139"):
        assert not _normalize_base(typed).startswith("https://https")
