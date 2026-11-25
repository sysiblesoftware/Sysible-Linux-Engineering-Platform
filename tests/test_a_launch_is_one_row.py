"""A launched pipeline is one thing that happened, and it was listed as three.

Reported as: runs should encompass all of the steps, not a different run per
step. Launching "terraform → ansible → enroll" wrote three run rows, and the
Runs table listed them as three siblings -- newest first, so in reverse order,
each with its own number and its own status, with nothing on the page saying
they were one launch. Answering "did that deploy work" meant reading three rows
and doing the join by hand.

Nothing had to be collected to fix it. _dispatch_pipeline already stamps every
step of a launch with the same group_id -- it is how the step strip inside a run
draws the chain -- and list_runs does SELECT *, so the column was in the payload
the table was already fetching. The list simply never read it.
"""
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src" / "views"
RUNS = (SRC / "Runs.jsx").read_text(encoding="utf-8")
APP = (pathlib.Path(__file__).resolve().parents[1] / "backend" / "app.py").read_text(encoding="utf-8")
DB = (pathlib.Path(__file__).resolve().parents[1] / "backend" / "db.py").read_text(encoding="utf-8")


def test_the_backend_really_does_stamp_a_launch():
    """The premise. If pipelines stopped sharing a group_id the grouping below
    would silently go back to one row per step."""
    assert "group_id = secrets.token_hex" in APP, "a pipeline no longer mints a group id"
    assert re.search(r"group_id=group_id", APP), "the steps are not stamped with it"


def test_the_column_reaches_the_list_without_being_asked_for():
    """list_runs does SELECT *, which is why no API change was needed."""
    body = DB[DB.index("def list_runs("):][:1400]
    assert 'SELECT * FROM runs' in body
    assert 'ADD COLUMN group_id' in DB


def test_the_table_groups_by_it():
    assert "function groupRuns" in RUNS, "the list does not group at all"
    body = RUNS[RUNS.index("function groupRuns"):RUNS.index("function pipelineStatus")]
    assert "group_id" in body


def test_a_run_with_no_group_stays_its_own_row():
    """An ad-hoc play is not a pipeline, and a group of one is not either."""
    body = RUNS[RUNS.index("function groupRuns"):RUNS.index("function pipelineStatus")]
    assert "solo" in body, "ungrouped runs are folded in with the pipelines"


def test_the_steps_are_shown_in_the_order_they_ran():
    """The list arrives newest-first. A pipeline read backwards is worse than
    three rows, because it looks like it was ordered on purpose."""
    body = RUNS[RUNS.index("function groupRuns"):RUNS.index("function pipelineStatus")]
    assert re.search(r"sort\(\(a, b\) => a\.id - b\.id\)", body), \
        "the steps are not put back into launch order"


@pytest.mark.parametrize("statuses,want", [
    (["success", "success", "success"], "success"),
    (["success", "failed", "canceled"], "failed"),
    (["success", "running", "queued"], "running"),
    (["success", "canceled", "canceled"], "canceled"),
    (["success", "success", "queued"], "queued"),
])
def test_the_pipeline_takes_the_worst_thing_that_happened_to_it(statuses, want):
    """Run #144 is the case: a failed ansible step followed by a canceled
    enroll. Reading the LAST step would have called that launch 'canceled',
    which hides the failure that caused it."""
    body = RUNS[RUNS.index("function pipelineStatus"):RUNS.index("export default function Runs")]
    order = []
    for m in re.finditer(r"has\('(\w+)'\)", body):
        order.append(m.group(1))
    # running before failed before canceled before queued; success only if all are
    assert order[:4] == ["running", "failed", "canceled", "queued"], order
    # and the parametrised expectation must be reachable from that ordering
    for s in order:
        if s in statuses:
            assert s == want, f"{statuses} should read as {want}, got {s}"
            break
    else:
        assert want == "success" and all(s == "success" for s in statuses)


def test_clicking_the_row_opens_the_step_worth_reading():
    """Not step one. A failed launch is opened at the step that failed."""
    body = RUNS[RUNS.index("const landing"):][:260]
    assert "status === 'failed'" in body, "it does not prefer the failed step"
    assert "steps[steps.length - 1]" in body, "and has no fallback when none failed"


def test_each_step_is_still_reachable_and_re_runnable():
    """Grouping must not take anything away: every step keeps its own link and
    its own Re-run."""
    body = RUNS[RUNS.index("{expanded && steps.map"):][:900]
    assert "onOpen(r.id)" in body, "a step inside a group cannot be opened"
    assert "rerun(r.id)" in body, "a step inside a group cannot be re-run"
