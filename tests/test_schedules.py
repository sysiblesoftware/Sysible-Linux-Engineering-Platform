"""Recurring runs: schedule CRUD, next-run computation, the due→fire→advance
cycle, and the health-warnings endpoint."""
import time

import backend.db as db


def test_compute_next_run_is_in_the_future():
    now = time.time()
    for cadence in ("hourly", "daily", "weekly"):
        nxt = db.compute_next_run(cadence, "02:00", 0, now_ts=now)
        assert nxt > now


def test_weekly_lands_on_the_requested_weekday():
    import datetime
    nxt = db.compute_next_run("weekly", "09:00", 2)  # Wednesday
    assert datetime.datetime.fromtimestamp(nxt).weekday() == 2


def test_schedule_crud(client, project):
    inv = client.post("/inventories", json={"name": "sched-inv"}).json()["id"]
    r = client.post("/schedules", json={
        "name": "Nightly", "project_id": project["id"], "kind": "ansible",
        "target": "site.yml", "inventory_id": inv, "cadence": "daily", "at": "03:30"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["next_run"] > time.time()

    # Appears in the list.
    assert any(s["id"] == sid for s in client.get("/schedules").json()["schedules"])

    # Pause it.
    assert client.patch(f"/schedules/{sid}", json={"enabled": 0}).json()["enabled"] == 0
    # Bad cadence is rejected.
    assert client.patch(f"/schedules/{sid}", json={"cadence": "yearly"}).status_code == 400

    assert client.delete(f"/schedules/{sid}").json()["status"] == "deleted"
    assert all(s["id"] != sid for s in client.get("/schedules").json()["schedules"])


def test_due_and_fire_advances_next_run(client, project):
    inv = client.post("/inventories", json={"name": "sched-inv2"}).json()["id"]
    sid = client.post("/schedules", json={
        "project_id": project["id"], "kind": "ansible", "target": "site.yml",
        "inventory_id": inv, "cadence": "daily", "at": "02:00"}).json()["id"]
    # Force it due by backdating next_run.
    with db._connect() as c:
        c.execute("UPDATE schedules SET next_run=? WHERE id=?", (time.time() - 10, sid))
    due_ids = [s["id"] for s in db.due_schedules()]
    assert sid in due_ids
    # Simulate the scheduler firing it.
    db.mark_schedule_fired(sid, 999, status="launched")
    s = db.get_schedule(sid)
    assert s["last_run_id"] == 999 and s["last_status"] == "launched"
    assert s["next_run"] > time.time()          # advanced to a future slot
    assert sid not in [x["id"] for x in db.due_schedules()]


def test_health_warnings_shape(client):
    d = client.get("/health-warnings").json()
    assert "warnings" in d and isinstance(d["warnings"], list)
    for w in d["warnings"]:
        assert {"id", "severity", "title"} <= set(w)
