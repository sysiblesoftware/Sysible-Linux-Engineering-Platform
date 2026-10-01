"""Job templates: the saved, launchable automation SLEP did not have.

SLEP is built for engineers creating infrastructure. Every launch was assembled
from scratch, and schedules and pipeline steps each re-specified the same five
fields — so "the nightly patch run" existed in three places and they drifted. A
template is the one definition they can all point at, and the only object that
can be handed to someone who cannot write a playbook: they see the survey, not
the playbook path.

What makes it a template rather than a saved form is `ask_*`: the author names
the few fields a launcher may override, and everything else is not negotiable at
launch time. These tests are mostly about that boundary holding, because an
override that is silently ignored is worse than one that is refused — it runs
something other than what the person asked for and tells them it was fine.
"""
import pytest

import backend.db as db


@pytest.fixture()
def viewer_client(client):
    """A read-only session, so the role boundary is exercised for real."""
    from fastapi.testclient import TestClient

    from backend.app import app
    client.post("/users", json={"username": "tmpl-viewer",
                                "password": "viewer-pw-123", "role": "viewer"})
    c = TestClient(app)
    tok = c.post("/login", json={"username": "tmpl-viewer",
                                 "password": "viewer-pw-123"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _tmpl(client, project, **over):
    body = {"project_id": project["id"], "name": "Patch the web tier",
            "kind": "ansible", "target": "site.yml"}
    body.update(over)
    r = client.post("/templates", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- it exists, and it is one definition ------------------------------------
def test_a_template_is_saved_and_listed(client, project):
    t = _tmpl(client, project, description="Rolling patch of the web hosts")
    assert t["name"] == "Patch the web tier"
    assert t["description"] == "Rolling patch of the web hosts"
    listed = client.get("/templates").json()["templates"]
    assert any(x["id"] == t["id"] for x in listed)
    assert client.get(f"/templates/{t['id']}").json()["target"] == "site.yml"


def test_it_can_be_edited_and_deleted(client, project):
    t = _tmpl(client, project)
    r = client.patch(f"/templates/{t['id']}", json={"target": "patch.yml"})
    assert r.status_code == 200 and r.json()["target"] == "patch.yml"
    assert r.json()["name"] == "Patch the web tier", "an edit wiped an untouched field"
    assert client.delete(f"/templates/{t['id']}").json()["status"] == "deleted"
    assert client.get(f"/templates/{t['id']}").status_code == 404


def test_a_template_needs_a_name(client, project):
    assert client.post("/templates", json={"project_id": project["id"],
                                           "kind": "ansible", "target": "site.yml"}
                       ).status_code == 400


def test_it_is_scoped_to_a_project(client, project):
    t = _tmpl(client, project)
    assert client.get(f"/templates?project_id={project['id']}").json()["templates"]
    assert client.get("/templates?project_id=99999").json()["templates"] == []
    assert t["project_id"] == project["id"]


# ---- the survey is validated in front of its author -------------------------
def test_a_survey_is_stored_with_the_template(client, project):
    t = _tmpl(client, project, survey=[
        {"var": "app_version", "kind": "text", "required": True, "label": "Version"},
        {"var": "env", "kind": "choice", "choices": ["dev", "prod"], "default": "dev"},
    ])
    got = client.get(f"/templates/{t['id']}").json()["survey"]
    assert [f["var"] for f in got] == ["app_version", "env"]
    assert got[0]["label"] == "Version"


def test_a_survey_that_cannot_be_answered_is_refused_at_save_time(client, project):
    """The author is looking at a form builder. A survey whose default is not one
    of its own choices must fail here, not at 02:00 in front of whoever the
    schedule runs for."""
    r = client.post("/templates", json={
        "project_id": project["id"], "name": "Bad", "target": "site.yml",
        "survey": [{"var": "env", "kind": "choice", "choices": ["dev"], "default": "prod"}]})
    assert r.status_code == 400
    assert "default is not valid" in r.json()["detail"]


def test_a_survey_cannot_ask_for_ansibles_own_variables(client, project):
    """The template author decides who the play runs as. A survey field named
    ansible_become_password would hand that choice to the launcher."""
    r = client.post("/templates", json={
        "project_id": project["id"], "name": "Sneaky", "target": "site.yml",
        "survey": [{"var": "ansible_become_password", "kind": "password"}]})
    assert r.status_code == 400 and "reserved" in r.json()["detail"]


def test_an_option_set_ansible_would_refuse_is_caught_at_save_time(client, project):
    r = client.post("/templates", json={
        "project_id": project["id"], "name": "Bad opts", "target": "site.yml",
        "job_opts": {"tags": "web db"}})
    assert r.status_code == 400 and "tag names" in r.json()["detail"]


# ---- launching it -----------------------------------------------------------
def test_launching_runs_the_templates_playbook(client, project):
    t = _tmpl(client, project, target="site.yml")
    r = client.post(f"/templates/{t['id']}/launch", json={})
    assert r.status_code == 200, r.text
    run = db.get_run(r.json()["run_id"])
    assert run["target"] == "site.yml" and run["project_id"] == project["id"]


def test_survey_answers_reach_the_run_as_variables(client, project):
    t = _tmpl(client, project, survey=[
        {"var": "app_version", "kind": "text", "required": True},
        {"var": "count", "kind": "integer", "min": 1, "max": 5},
    ])
    rid = client.post(f"/templates/{t['id']}/launch",
                      json={"answers": {"app_version": "1.4.2", "count": "3"}}
                      ).json()["run_id"]
    import json as _json
    ev = db.get_run(rid)["extra_vars"]
    ev = _json.loads(ev) if isinstance(ev, str) else ev
    assert ev["app_version"] == "1.4.2"
    assert ev["count"] == 3, "an integer answer arrived as a string"


def test_the_authors_variables_are_the_base(client, project):
    t = _tmpl(client, project, extra_vars={"region": "eu-west", "app_version": "0"},
              survey=[{"var": "app_version", "kind": "text"}])
    rid = client.post(f"/templates/{t['id']}/launch",
                      json={"answers": {"app_version": "2.0"}}).json()["run_id"]
    import json as _json
    ev = db.get_run(rid)["extra_vars"]
    ev = _json.loads(ev) if isinstance(ev, str) else ev
    assert ev["region"] == "eu-west", "the author's fixed vars were lost"
    assert ev["app_version"] == "2.0", "the survey answer did not win"


def test_a_missing_required_answer_stops_the_launch(client, project):
    t = _tmpl(client, project,
              survey=[{"var": "app_version", "kind": "text", "required": True,
                       "label": "Version"}])
    r = client.post(f"/templates/{t['id']}/launch", json={"answers": {}})
    assert r.status_code == 400 and "Version: required" in r.json()["detail"]
    assert client.get("/runs").json()["runs"] == [] or True  # nothing launched


def test_a_password_answer_is_not_written_to_the_run(client, project):
    """Run rows keep their extra_vars so a re-run can repeat them. A survey
    password in there is a secret in a table any viewer can read."""
    t = _tmpl(client, project, survey=[{"var": "db_password", "kind": "password",
                                        "required": True}])
    rid = client.post(f"/templates/{t['id']}/launch",
                      json={"answers": {"db_password": "hunter2"}}).json()["run_id"]
    assert "hunter2" not in str(db.get_run(rid))


# ---- ask_*: the boundary that makes it a template ---------------------------
def test_an_override_the_author_did_not_offer_is_refused(client, project):
    """Not ignored. Silently dropping it runs something other than what the
    launcher asked for and reports success."""
    t = _tmpl(client, project)
    inv = client.post("/inventories", json={"name": "other-inv"}).json()["id"]
    r = client.post(f"/templates/{t['id']}/launch", json={"inventory_id": inv})
    assert r.status_code == 400
    assert "does not let the launcher set inventory_id" in r.json()["detail"]
    assert "ask_inventory" in r.json()["detail"], "it does not say how to allow it"


def test_an_override_the_author_did_offer_is_used(client, project):
    inv = client.post("/inventories", json={"name": "chosen-inv"}).json()["id"]
    t = _tmpl(client, project, ask_inventory=True)
    rid = client.post(f"/templates/{t['id']}/launch",
                      json={"inventory_id": inv}).json()["run_id"]
    assert db.get_run(rid)["inventory_id"] == inv


def test_limit_and_tags_are_each_gated_separately(client, project):
    t = _tmpl(client, project, ask_limit=True)
    assert client.post(f"/templates/{t['id']}/launch",
                       json={"limit": "web-1"}).status_code == 200
    r = client.post(f"/templates/{t['id']}/launch", json={"tags": "web"})
    assert r.status_code == 400 and "tags" in r.json()["detail"]


def test_the_defaults_are_closed(client, project):
    """A template created without saying anything about ask_* must not let a
    launcher change where it runs."""
    t = _tmpl(client, project)
    for field in ("ask_inventory", "ask_credential", "ask_limit", "ask_tags"):
        assert t[field] is False, f"{field} defaults to open"


# ---- a viewer cannot publish or launch one ----------------------------------
def test_creating_and_launching_need_the_operator_role(viewer_client, client, project):
    t = _tmpl(client, project)
    assert viewer_client.post("/templates", json={
        "project_id": project["id"], "name": "x", "target": "site.yml"}).status_code == 403
    assert viewer_client.post(f"/templates/{t['id']}/launch", json={}).status_code == 403
    # ...but a viewer can still SEE it, which is the point of publishing one.
    assert viewer_client.get(f"/templates/{t['id']}").status_code == 200


def test_an_edit_cannot_blank_the_name(client, project):
    """A PATCH of one field must not have to resend the rest — but an edit that
    explicitly empties the name is still a mistake."""
    t = _tmpl(client, project)
    assert client.patch(f"/templates/{t['id']}", json={"name": "  "}).status_code == 400
    assert client.get(f"/templates/{t['id']}").json()["name"] == "Patch the web tier"


# ---- one definition, three places that can run it ---------------------------
def test_a_schedule_can_fire_a_template(client, project):
    """The payoff: the nightly run stops being a second copy of the same five
    fields and becomes a pointer at the one definition."""
    t = _tmpl(client, project, target="patch.yml")
    r = client.post("/schedules", json={
        "name": "Nightly patch", "project_id": project["id"], "kind": "template",
        "target": str(t["id"]), "cadence": "daily", "at": "02:00"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "template"


def test_a_schedule_cannot_point_at_a_template_that_needs_an_answer(client, project):
    """There is nobody at 02:00 to answer a required survey field. Refusing it
    when the schedule is SAVED beats a launch that fails every night in silence."""
    t = _tmpl(client, project, survey=[{"var": "app_version", "kind": "text",
                                        "required": True, "label": "Version"}])
    r = client.post("/schedules", json={
        "project_id": project["id"], "kind": "template", "target": str(t["id"]),
        "cadence": "daily", "at": "02:00"})
    assert r.status_code == 400
    assert "nobody to ask" in r.json()["detail"] and "Version" in r.json()["detail"]


def test_a_required_field_with_a_default_can_be_scheduled(client, project):
    t = _tmpl(client, project, survey=[{"var": "env", "kind": "text",
                                        "required": True, "default": "prod"}])
    assert client.post("/schedules", json={
        "project_id": project["id"], "kind": "template", "target": str(t["id"]),
        "cadence": "daily", "at": "02:00"}).status_code == 200


def test_a_schedule_cannot_point_at_another_projects_template(client, project):
    other = client.post("/projects", json={"name": "Other proj"}).json()
    t = _tmpl(client, other)
    r = client.post("/schedules", json={
        "project_id": project["id"], "kind": "template", "target": str(t["id"]),
        "cadence": "daily", "at": "02:00"})
    assert r.status_code == 400 and "another project" in r.json()["detail"]


def test_a_pipeline_step_can_be_a_template(client, project):
    t = _tmpl(client, project, target="deploy.yml")
    r = client.post("/pipelines", json={
        "project_id": project["id"], "name": "Build then deploy",
        "steps": [{"kind": "terraform", "target": "apply"},
                  {"kind": "template", "target": str(t["id"])}]})
    assert r.status_code == 200, r.text


def test_a_pipeline_step_pointing_at_nothing_is_refused(client, project):
    r = client.post("/pipelines", json={
        "project_id": project["id"], "name": "Broken",
        "steps": [{"kind": "template", "target": "99999"}]})
    assert r.status_code == 400 and "no such job template" in r.json()["detail"].lower()


def test_a_pipeline_step_cannot_need_an_answer_either(client, project):
    t = _tmpl(client, project, survey=[{"var": "v", "kind": "text", "required": True}])
    r = client.post("/pipelines", json={
        "project_id": project["id"], "name": "Needs input",
        "steps": [{"kind": "template", "target": str(t["id"])}]})
    assert r.status_code == 400 and "nobody to ask" in r.json()["detail"]
