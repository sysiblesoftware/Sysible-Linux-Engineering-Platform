"""A run that has ended never renders as "Starting…".

Reported against Run #144: the Ansible step's pill read `failed` while its panel
read "Starting…" with `1 host(s) · 0 task(s)`, and the Enroll step read
`canceled` beside a badge that still said "enrolling…" and a line promising it
was "Registering this project's VMs into the Controller…". The operator's
question was the obvious one -- why did this fail, and where is what happened.

The cause was in the props. RunViz's panels were handed the PARSED model and
nothing else: no run status, no log. So when ansible-playbook died before
emitting its first task -- a bad playbook path, an inventory it could not read,
a credential it could not use -- the model held no tasks, no recap and no
hosts, and the panel had nothing to draw except the state it starts in. Both
facts it needed were already in scope at the call site and simply not passed.

Starting is the state a finished run is furthest from, and that moment is
exactly when the reason matters most.
"""
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src" / "views"
VIZ = (SRC / "RunViz.jsx").read_text(encoding="utf-8")
RUNS = (SRC / "Runs.jsx").read_text(encoding="utf-8")


def _fn(src, name):
    """A component's body, from its declaration to the next top-level one."""
    i = src.index(f"function {name}(")
    nxt = src.find("\nfunction ", i + 1)
    return src[i:nxt if nxt > 0 else len(src)]


def test_the_panels_are_given_the_status_and_the_log():
    """Both were already in scope where RunViz is rendered. Nothing could be
    reported without them."""
    for call in re.findall(r"<RunViz\b[^>]*/>", RUNS, re.S):
        assert "status=" in call, f"a RunViz is rendered without the run status: {call[:90]}"
        assert "text=" in call, f"a RunViz is rendered without the log: {call[:90]}"
    assert len(re.findall(r"<RunViz\b", RUNS)) >= 2, "the call sites moved; re-read this test"


def test_runviz_passes_them_down():
    body = _fn(VIZ, "RunViz")
    assert "status" in body and "text" in body
    for panel in ("AnsibleViz", "EnrollViz"):
        m = re.search(rf"<{panel}\b[^>]*/>", body, re.S)
        assert m, f"{panel} is no longer rendered by RunViz"
        assert "status=" in m.group(0) and "text=" in m.group(0), \
            f"{panel} is rendered without the status/log: {m.group(0)}"


@pytest.mark.parametrize("panel,what", [("AnsibleViz", "any task ran"),
                                        ("EnrollViz", "it registered anything")])
def test_a_terminal_run_with_nothing_parsed_shows_why(panel, what):
    body = _fn(VIZ, panel)
    assert "ENDED.has(status)" in body, f"{panel} never looks at whether the run ended"
    assert "WhyItEnded" in body, f"{panel} has nothing to show in place of the diagram"
    assert what in body


@pytest.mark.parametrize("panel", ["AnsibleViz", "EnrollViz"])
def test_the_headline_never_says_starting_after_it_ended(panel):
    """Even when SOME of it parsed -- a play that reached three hosts and died
    still has no currentTask, and the headline fell through to 'Starting…'."""
    body = _fn(VIZ, panel)
    starting = [ln for ln in body.splitlines() if "'Starting…'" in ln]
    assert starting, f"{panel} no longer has the headline this guards"
    window = body[body.index("'Starting…'") - 400:body.index("'Starting…'")]
    assert "ENDED.has(status)" in window, \
        f"{panel} can still headline a finished run as starting"


def test_the_enroll_panel_stops_promising_work_after_it_ended():
    body = _fn(VIZ, "EnrollViz")
    i = body.index("Registering this project")
    assert "!ENDED.has(status)" in body[max(0, i - 300):i], \
        "a canceled enroll still says it is registering VMs"


def test_the_reason_prefers_lines_that_say_something():
    """The tail of a log is better than nothing, but a fatal/ERROR! line is the
    answer. If the filter ever stops matching, this falls back to the tail
    silently -- which is why the filter is pinned rather than the output."""
    body = VIZ[VIZ.index("function whyItEnded"):]
    body = body[:body.index("function WhyItEnded")]
    for marker in ("ERROR!", "fatal:", "unreachable"):
        assert marker in body, f"{marker} is no longer recognised as a reason"
    assert "slice(-8)" in body, "it no longer bounds how much it shows"
    assert "u001b" in body or "\\x1b" in body, "ANSI colour codes are not stripped"


def test_canceled_and_failed_both_count_as_ended():
    """Run #144 had one of each, and only one of them was being handled."""
    block = VIZ[VIZ.index("const ENDED"):][:200]
    for s in ("failed", "canceled"):
        assert f"'{s}'" in block, f"{s} is not treated as a finished run"
