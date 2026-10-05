"""The new console views call endpoints that exist, with fields that exist.

These views are hand-written JSX against a hand-written API, and there is no JS
test runner here, so the realistic failure is not a rendering bug — it is a view
calling `api('template')`, or reading `d.items` from an endpoint that returns
`{"templates": …}`, or POSTing `ask_inv` when the field is `ask_inventory`. Every
one of those builds cleanly, ships, and fails the first time somebody clicks.

So this walks the actual route table and the actual response bodies and checks
the views against them.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
VIEWS = {
    "Templates": SRC / "views" / "Templates.jsx",
    "Notifications": SRC / "views" / "Notifications.jsx",
    "ProjectContent": SRC / "views" / "ProjectContent.jsx",
}


@pytest.fixture(scope="module")
def routes():
    from backend.app import app
    out = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        for m in getattr(r, "methods", []) or []:
            out.add((m, path))
    return out


def _api_calls(text):
    """Every api('path', {method}) in a view, as (METHOD, path-with-{id})."""
    calls = []
    for m in re.finditer(r"api\(\s*(`[^`]+`|'[^']+')\s*(?:,\s*\{([^}]*)\})?", text):
        raw = m.group(1).strip("`'")
        opts = m.group(2) or ""
        method = "GET"
        mm = re.search(r"method:\s*'(\w+)'", opts)
        if mm:
            method = mm.group(1)
        # `templates/${t.id}/launch` -> templates/{id}/launch
        path = re.sub(r"\$\{[^}]+\}", "{id}", raw)
        calls.append((method, "/" + path.lstrip("/")))
    return calls


@pytest.mark.parametrize("name", sorted(VIEWS))
def test_every_endpoint_a_view_calls_exists(name, routes):
    text = VIEWS[name].read_text(encoding="utf-8")
    known = {(m, re.sub(r"\{[^}]+\}", "{id}", p)) for m, p in routes}
    missing = [c for c in _api_calls(text) if tuple(c) not in known]
    assert not missing, f"{name}.jsx calls endpoints that do not exist: {missing}"


def test_the_templates_view_reads_the_key_the_api_returns(client, project):
    """`d.templates` — not `d.items`, not a bare list."""
    body = client.get("/templates").json()
    assert "templates" in body
    assert "d.templates" in VIEWS["Templates"].read_text(encoding="utf-8")


def test_the_notifications_view_reads_the_key_the_api_returns(client):
    body = client.get("/notifications").json()
    assert "notifications" in body
    assert "d.notifications" in VIEWS["Notifications"].read_text(encoding="utf-8")


def test_every_template_field_the_editor_sends_is_one_the_api_accepts(client, project):
    """A field the API silently drops is a setting that looks saved and is not."""
    import backend.db as db
    sent = {
        "project_id": project["id"], "name": "Contract", "description": "d",
        "kind": "ansible", "target": "site.yml",
        "inventory_id": None, "credential_id": None,
        "extra_vars": {"a": 1}, "job_opts": {"tags": "web"},
        "survey": [{"var": "v", "kind": "text"}],
        "ask_inventory": True, "ask_credential": True,
        "ask_limit": True, "ask_tags": True,
    }
    t = client.post("/templates", json=sent).json()
    for k, v in sent.items():
        if k == "project_id":
            continue
        if k == "survey":
            # Normalised on save — label/help/required are filled in. What matters
            # is that nothing the editor sent was LOST.
            for i, f in enumerate(v):
                assert f.items() <= t["survey"][i].items(), \
                    f"the API dropped part of survey field {i}: {f} vs {t['survey'][i]}"
            continue
        assert t[k] == v, f"the API dropped or changed '{k}': sent {v!r}, got {t.get(k)!r}"
    assert set(sent) - {"project_id"} <= set(db._TEMPLATE_FIELDS) | {"kind"}


def test_the_editor_sends_exactly_the_ask_flags_the_api_knows(client):
    """The four checkboxes are named in the JSX; a fifth invented one would be
    silently ignored, and a renamed one would stop working."""
    import backend.db as db
    text = VIEWS["Templates"].read_text(encoding="utf-8")
    in_view = set(re.findall(r"'(ask_\w+)'", text))
    assert in_view == {"ask_inventory", "ask_credential", "ask_limit", "ask_tags"}
    assert in_view <= set(db._TEMPLATE_FIELDS)


def test_the_launch_dialog_sends_only_fields_the_api_offers(client, project):
    """The API REFUSES an override the template did not open. A view that sent
    one unconditionally would turn every launch into a 400."""
    text = VIEWS["Templates"].read_text(encoding="utf-8")
    assert "answers" in text
    pairs = re.findall(r"\['(\w+)', '(ask_\w+)'\]", text)
    assert dict(pairs) == {"inventory_id": "ask_inventory", "credential_id": "ask_credential",
                           "limit": "ask_limit", "tags": "ask_tags"}, pairs

    t = client.post("/templates", json={"project_id": project["id"], "name": "L",
                                        "target": "site.yml"}).json()
    r = client.post(f"/templates/{t['id']}/launch", json={"inventory_id": 1})
    assert r.status_code == 400, "the API stopped refusing an unopened override"


def test_the_survey_field_kinds_the_builder_offers_are_the_ones_that_work():
    from backend import surveys
    text = VIEWS["Templates"].read_text(encoding="utf-8")
    block = text[text.index("const FIELD_KINDS"):text.index("const needsChoices")]
    offered = set(re.findall(r"\['(\w+)',", block))
    assert offered == set(surveys.KINDS), \
        f"the builder offers field kinds the backend does not implement: {offered ^ set(surveys.KINDS)}"


def test_the_notification_kinds_the_editor_offers_are_implemented():
    from backend import notifications
    text = VIEWS["Notifications"].read_text(encoding="utf-8")
    offered = set(re.findall(r'<option value="(\w+)">', text))
    assert {"webhook", "email"} <= offered
    assert offered <= set(notifications.KINDS) | {"", "all"}


def test_a_blank_secret_on_edit_means_keep_not_clear(client):
    """The editor omits an untouched secret. If the API read that as "clear it",
    renaming a rule would silently unsign every webhook."""
    import backend.db as db
    text = VIEWS["Notifications"].read_text(encoding="utf-8")
    assert "f.secret ? { secret: f.secret } : {}" in text, \
        "the editor now always sends the secret field"
    n = client.post("/notifications", json={
        "name": "n1", "kind": "webhook",
        "config": {"url": "https://h/x", "secret": "s3cret"}}).json()
    before = db.get_notification(n["id"], reveal=True)["config"]["secret"]
    client.patch(f"/notifications/{n['id']}", json={"name": "renamed",
                                                    "config": {"url": "https://h/x"}})
    assert db.get_notification(n["id"], reveal=True)["config"]["secret"] == before


def test_the_views_are_reachable_from_the_navigation():
    """A view nobody can click is a view nobody has."""
    app_jsx = (SRC / "App.jsx").read_text(encoding="utf-8")
    for key, comp in (("templates", "Templates"), ("notifications", "Notifications")):
        assert f"key: '{key}'" in app_jsx, f"{key} is not in the nav"
        assert f"import {comp} from './views/{comp}.jsx'" in app_jsx
        assert f"view === '{key}'" in app_jsx, f"{key} is in the nav but renders nothing"


# ------------------------------------------------- roles, collections and molecule
def test_the_content_view_reads_the_keys_the_api_returns(client, project):
    """`requirements` / `installed`, and the sub-keys under them."""
    body = client.get(f"/projects/{project['id']}/content").json()
    assert set(body) == {"requirements", "installed"}
    assert set(body["requirements"]) >= {"roles", "collections", "errors"}
    assert set(body["installed"]) == {"roles", "collections"}

    text = VIEWS["ProjectContent"].read_text(encoding="utf-8")
    for key in ("requirements", "installed", "errors", "roles", "collections"):
        assert key in text, f"the view never reads '{key}'"


def test_the_molecule_view_reads_the_fields_the_api_returns(client, project, tmp_path):
    import backend.db as db
    root = db.project_dir(project["id"])
    (root / "molecule" / "default").mkdir(parents=True, exist_ok=True)
    (root / "molecule" / "default" / "molecule.yml").write_text(
        "driver:\n  name: delegated\n", encoding="utf-8")

    body = client.get(f"/projects/{project['id']}/molecule").json()
    assert "scenarios" in body and body["scenarios"], body
    got = body["scenarios"][0]
    assert set(got) == {"name", "driver", "runnable", "reason"}

    text = VIEWS["ProjectContent"].read_text(encoding="utf-8")
    assert "d.scenarios" in text
    for f in ("s.name", "s.driver", "s.runnable", "s.reason"):
        assert f in text, f"the view never reads {f}"


def test_a_requirement_entry_uses_only_fields_galaxy_emits():
    """The row renders name/version/src/source. A field the backend never sets would
    render blank forever and nobody would notice."""
    import inspect

    from backend import galaxy
    emitted = set(re.findall(r'"(\w+)"', re.search(
        r"for k in \(([^)]*)\)", inspect.getsource(galaxy._entries)).group(1)))

    # Only the requirement ROW. Searching the whole file picks up the `e` of
    # `catch (e)` and `onChange={(e) => …}`, which are not entries at all.
    text = VIEWS["ProjectContent"].read_text(encoding="utf-8")
    block = text[text.index("declared(req).map("):text.index("{extra.length > 0")]
    used = set(re.findall(r"\be\.(\w+)\b", block))
    assert used, "the row block no longer reads any entry field — has it moved?"
    assert used <= emitted, f"the view reads fields galaxy never emits: {used - emitted}"


def test_launching_a_molecule_scenario_uses_the_runs_endpoint(client, project, routes):
    """Not an invented /molecule/run — the generic launcher, with kind=molecule."""
    text = VIEWS["ProjectContent"].read_text(encoding="utf-8")
    assert "kind: 'molecule'" in text
    assert ("POST", "/runs") in routes
    import backend.app as app_mod
    assert "molecule" in app_mod.RUNNERS


def test_a_blank_galaxy_token_on_save_means_keep_not_clear(client, project):
    """The editor omits an untouched token. If the API read that as "clear it",
    renaming a server list would silently drop the credential."""
    import backend.db as db
    text = VIEWS["ProjectContent"].read_text(encoding="utf-8")
    assert "...(token ? { token } : {})" in text, "the view now always sends the token"

    pid = project["id"]
    client.post(f"/projects/{pid}/content/galaxy",
                json={"servers": "https://hub.example/api/galaxy/", "token": "s3cret"})
    before = db.get_project(pid)["galaxy_token"]
    assert before
    client.post(f"/projects/{pid}/content/galaxy", json={"servers": "https://other/"})
    assert db.get_project(pid)["galaxy_token"] == before, "the token was dropped"


def test_the_panels_are_reachable_from_the_project_menu():
    """A panel nobody can click is a panel nobody has."""
    projects = (SRC / "views" / "Projects.jsx").read_text(encoding="utf-8")
    assert "import ProjectContent from './ProjectContent.jsx'" in projects
    assert "Roles & collections" in projects and "Molecule tests" in projects
    assert "<ProjectContent" in projects
    assert "onOpenRun={onOpenRun}" in projects, "a launched run would go nowhere"
