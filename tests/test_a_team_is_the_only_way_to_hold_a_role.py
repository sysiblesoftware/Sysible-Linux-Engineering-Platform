"""A role in an organization comes from a team, and from nowhere else.

There used to be two grants: a direct row in org_members, and membership of a
team whose org_role conferred one. The effective role was the higher of the
two, so a team could only ever restate what the member row already said -- a
second place to look, and no capability. Reported as:

    "I have to add the user under users then I add them as a team member but
     it's not really rbac"

which is a fair reading. Now there is one place to grant and one to revoke, and
the member list is a VIEW of who holds what and where it came from.

The migration matters more than the feature. Everyone currently holding a
direct role would simply lose it -- and the people that hits hardest are the
administrators, who would have no way back in.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import backend.db as db  # noqa: E402


# The cast. team_members.username is a foreign key to admins, so a team cannot
# hold someone who does not exist -- which is also why the team member box is a
# picker now rather than free text: a typo there failed on a constraint, with
# nothing to check the name against.
PEOPLE = ("ghost", "bob", "carol", "dave", "legacy-admin", "legacy-op",
          "moved", "erin", "frank", "gail", "hank", "ivy")


@pytest.fixture(autouse=True)
def _people(client):
    """Real accounts for everyone these tests grant roles to, removed after."""
    made = []
    for name in PEOPLE:
        if db.get_admin(name) is None:
            db.add_admin(name, "x" * 64, "salt", role="operator")
            made.append(name)
    yield
    for name in made:
        db.delete_admin(name)


def _direct_row(org_id, username, role):
    """Write the OLD kind of grant, behind the public helpers, so the tests
    below can ask what it is worth on its own."""
    with db._connect() as c:
        c.execute("INSERT INTO org_members(org_id, username, role) VALUES(?,?,?) "
                  "ON CONFLICT(org_id, username) DO UPDATE SET role=excluded.role",
                  (org_id, username, role))


@pytest.fixture()
def org(client):
    """Depends on `client` for its side effect: app startup runs init_db(), and
    without it there is no schema to create an organization in."""
    oid = db.create_org(f"T{os.urandom(4).hex()}", f"t-{os.urandom(4).hex()}")
    yield oid
    db.delete_org(oid)


# --------------------------------------------------------------- the rule

def test_a_direct_org_row_confers_nothing_on_its_own(org):
    """The whole change in one assertion. A row in org_members used to BE the
    grant; now it records that someone belongs and carries no authority."""
    _direct_row(org, "ghost", "admin")
    assert db.effective_org_role("ghost", org) is None
    assert org not in db.orgs_for_user("ghost")


def test_a_team_confers_its_role(org):
    tid = db.create_team(org, "Builders", "operator")
    db.add_team_member(tid, "bob")
    assert db.effective_org_role("bob", org) == "operator"
    assert org in db.orgs_for_user("bob")


def test_the_highest_team_wins(org):
    low = db.create_team(org, "Readers", "viewer")
    high = db.create_team(org, "Owners", "admin")
    db.add_team_member(low, "carol")
    db.add_team_member(high, "carol")
    assert db.effective_org_role("carol", org) == "admin"


def test_leaving_the_only_team_removes_the_role(org):
    tid = db.create_team(org, "Builders", "operator")
    db.add_team_member(tid, "dave")
    db.remove_team_member(tid, "dave")
    assert db.effective_org_role("dave", org) is None


# --------------------------------------------------------------- migration

def test_an_existing_direct_grant_survives_the_upgrade(org):
    """Nobody loses access. The row written before this change is converted into
    the team that confers the same role."""
    _direct_row(org, "legacy-admin", "admin")
    _direct_row(org, "legacy-op", "operator")
    with db._connect() as c:
        db._migrate_org_grants_to_teams(c)
    assert db.effective_org_role("legacy-admin", org) == "admin"
    assert db.effective_org_role("legacy-op", org) == "operator"


def test_the_migration_is_idempotent_and_does_not_drag_people_back(org):
    """It runs on every boot. Someone moved by hand since the last one must stay
    moved -- the migration may add, never remove."""
    _direct_row(org, "moved", "viewer")
    with db._connect() as c:
        db._migrate_org_grants_to_teams(c)
    promoted = db.create_team(org, "Owners", "admin")
    db.add_team_member(promoted, "moved")
    for _boot in range(3):
        with db._connect() as c:
            db._migrate_org_grants_to_teams(c)
    assert db.effective_org_role("moved", org) == "admin", \
        "a later boot dragged the user back to whatever org_members last said"


# ------------------------------------------------- the member list is a view

def test_the_member_list_says_which_team_grants_the_role(org):
    tid = db.create_team(org, "Builders", "operator")
    db.add_team_member(tid, "erin")
    row = next(m for m in db.list_org_members(org) if m["username"] == "erin")
    assert row["role"] == "operator"
    assert row["via"] == ["Builders"], row


def test_someone_in_no_team_is_not_in_the_member_list(org):
    _direct_row(org, "ghost", "operator")
    assert [m["username"] for m in db.list_org_members(org)] == []


# ------------------------------------------------------- grant and revoke

def test_setting_a_role_moves_the_user_between_the_standard_teams(org):
    db.set_org_member(org, "frank", "operator")
    assert db.effective_org_role("frank", org) == "operator"
    db.set_org_member(org, "frank", "viewer")
    assert db.effective_org_role("frank", org) == "viewer", \
        "the old grant was left in place, so the role never came down"


def test_setting_a_role_leaves_an_orgs_own_teams_alone(org):
    """Only the standard teams are rearranged. Dropping someone out of a team
    the org made, because a role was set somewhere else, is exactly the
    action-at-a-distance this change exists to remove."""
    mine = db.create_team(org, "Platform", "viewer")
    db.add_team_member(mine, "gail")
    db.set_org_member(org, "gail", "operator")
    assert "gail" in db.list_team_members(mine)


def test_removing_a_member_revokes_every_team_including_custom_ones(org):
    """A revoke that left them in one team would leave the role standing while
    the member list showed them gone."""
    mine = db.create_team(org, "Platform", "admin")
    db.add_team_member(mine, "hank")
    db.set_org_member(org, "hank", "operator")
    db.remove_org_member(org, "hank")
    assert db.effective_org_role("hank", org) is None
    assert "hank" not in db.list_team_members(mine)
    assert [m["username"] for m in db.list_org_members(org)] == []


def test_every_org_has_teams_to_grant_with(org):
    """'Give this person operator' needs a team that means it."""
    roles = {t["org_role"] for t in db.list_teams(org)} if db.list_teams(org) else set()
    db.ensure_standard_teams(org)
    roles = {t["org_role"] for t in db.list_teams(org)}
    assert {"admin", "operator", "viewer"} <= roles, roles


# ------------------------------------------------------------- through HTTP

def test_the_route_still_grants_and_now_does_it_through_a_team(client):
    """The existing endpoint keeps working -- it is how a role is given -- but
    what it writes is team membership."""
    import backend.db as _db
    oid = _db.create_org("Routed", f"routed-{os.urandom(3).hex()}")
    try:
        r = client.post(f"/organizations/{oid}/members",
                        json={"username": "ivy", "role": "operator"})
        assert r.status_code in (200, 201), r.text
        assert _db.effective_org_role("ivy", oid) == "operator"
        row = next(m for m in client.get(f"/organizations/{oid}/members").json()["members"]
                   if m["username"] == "ivy")
        assert row["via"], "the member list does not say where the role came from"
    finally:
        _db.delete_org(oid)
