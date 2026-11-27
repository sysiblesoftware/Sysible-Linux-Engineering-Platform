"""The SLEP console builds its tables by assigning innerHTML.

$() parses its argument as HTML. Every value interpolated into one of those
template strings is therefore markup, not text -- and the inventory tables carry
host names, addresses and groups IMPORTED FROM THE CONTROLLER, i.e. strings a
managed host can influence, on a page that also lists SSH credentials.

Two guards, because either alone can pass while the bug is present:

  * the static one reads the real file and insists every interpolation of a data
    object goes through esc(). It catches a NEW unescaped column, which is how
    this would come back.
  * the browser one runs the REAL $() and esc() out of that file against a
    payload and asserts nothing executed. It catches esc() being wrong, which
    the static guard cannot see.
"""
import json
import os
import re
import shutil
import subprocess
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "..", "webgui", "static", "index.html")


def _src():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


# The identifiers that hold SERVER data in this file. Interpolating one of these
# without esc() is the defect. `label`, `btn` and labeled()'s `t` are this
# file's own literals and are deliberately absent.
DATA_VARS = ("p", "f", "i", "c", "r", "inv", "h", "STATE")


def _interpolations():
    """Yield (line_number, expression) for every ${...} inside a $(`...`)."""
    out = []
    for n, line in enumerate(_src().splitlines(), 1):
        if "$(`" not in line and "${" not in line:
            continue
        for expr in re.findall(r"\$\{([^{}]*)\}", line):
            out.append((n, expr.strip()))
    return out


def test_every_data_interpolation_is_escaped():
    bad = []
    for n, expr in _interpolations():
        root = re.match(r"([A-Za-z_$][A-Za-z0-9_$]*)", expr)
        if not root or root.group(1) not in DATA_VARS:
            continue
        # A ternary on a fixed enum that yields only this file's own literals is
        # markup by intent (the dir/file icon). Anything that reaches a property
        # of a data object must be escaped.
        if "esc(" in expr:
            continue
        if re.search(r"===\s*'[a-z]+'\s*\?\s*'[^']*'\s*:\s*'[^']*'", expr):
            continue
        bad.append(f"  line {n}: ${{{expr}}}")
    assert not bad, (
        "these interpolate server data into innerHTML without esc(), so the "
        "value is parsed as HTML:\n" + "\n".join(bad))


def test_the_helper_is_actually_defined():
    src = _src()
    assert re.search(r"const\s+esc\s*=", src), "esc() is gone; the escaping above is a no-op"


def _helpers():
    """Pull $() and esc() straight out of the shipped file, so this exercises
    what runs rather than a copy that can drift."""
    src = _src()
    dollar = re.search(r"^\s*(const \$ = \(h\) =>.*?;)\s*$", src, re.M)
    assert dollar, "could not find the $() helper in index.html"
    esc = re.search(r"(const esc = \(v\) =>.*?\[c\]\)\);)", src, re.S)
    assert esc, "could not find the esc() helper in index.html"
    return dollar.group(1), esc.group(1)


PAYLOAD = '<img src=x onerror="window.__xss=1">'


def test_esc_encodes_every_character_that_can_break_out():
    """The static guard above proves esc() is CALLED. This proves it WORKS --
    run the real one, in node, against each metacharacter. Without this, esc()
    could be changed to the identity function and the static guard would still
    pass.
    """
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node is not installed on this runner")
    _, esc = _helpers()
    prog = esc + """
const cases = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
for (const [raw, want] of Object.entries(cases)) {
  const got = esc(raw);
  if (got !== want) { console.log('BAD ' + JSON.stringify(raw) + ' -> ' + JSON.stringify(got)); process.exit(1); }
}
if (esc(null) !== '' || esc(undefined) !== '') { console.log('BAD nullish'); process.exit(1); }
if (esc(%s).includes('<')) { console.log('BAD payload still has <'); process.exit(1); }
console.log('OK');
""" % json.dumps(PAYLOAD)
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "OK", (
        f"esc() does not neutralise its input:\n{out.stdout}\n{out.stderr}")


def _chromium():
    """The browser this environment actually ships, if any.

    Playwright's own default build is often absent (the image carries one
    chromium, not every channel), so look for it by path and skip rather than
    fail -- the two guards above already cover the defect on every runner.
    """
    for p in ("/opt/pw-browsers/chromium",
              os.environ.get("SYSIBLE_CHROMIUM", ""),
              shutil.which("chromium") or "",
              shutil.which("google-chrome") or ""):
        if p and os.path.exists(p):
            return p
    return None


def test_a_malicious_host_name_renders_as_text_not_script():
    """End to end, in a real HTML parser: the payload must not execute, must not
    become an element, and must still be READABLE -- the operator needs to see
    what the host called itself.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    exe = _chromium()
    if not exe:
        pytest.skip("no chromium on this runner")
    dollar, esc = _helpers()
    page_src = (
        '<!doctype html><meta charset="utf-8"><body><table><tbody id="tb"></tbody></table>\n'
        "<script>\n" + dollar + "\n" + esc + "\n"
        "var h = " + json.dumps({"name": PAYLOAD, "address": "10.0.0.1",
                                 "groups": "", "source": "controller"}) + ";\n"
        "var tr = $(`<tr><td>${esc(h.name)}</td><td class=\"muted\">${esc(h.address)}</td>"
        "<td class=\"muted\">${esc(h.groups||'')}</td><td class=\"muted\">${esc(h.source)}</td></tr>`);\n"
        "document.getElementById('tb').appendChild(tr);\n"
        "</script></body>")

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.set_content(page_src)
            assert page.evaluate("window.__xss === undefined"), (
                "the payload EXECUTED: esc() did not neutralise it")
            assert page.evaluate("document.querySelectorAll('#tb img').length") == 0, (
                "the payload was parsed as an <img> element rather than shown as text")
            assert page.evaluate("document.querySelector('#tb td').textContent") == PAYLOAD, (
                "the host name should appear literally, so the operator can SEE "
                "what the host called itself")
        finally:
            browser.close()
