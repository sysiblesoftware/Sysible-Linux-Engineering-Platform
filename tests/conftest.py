"""Point SLEP at a throwaway data dir BEFORE backend.db is imported (its paths
are resolved at import time from SLEP_DATA_DIR), then hand tests an authed
TestClient."""
import os
import tempfile

os.environ.setdefault("SLEP_DATA_DIR", tempfile.mkdtemp(prefix="slep-test-"))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from backend.app import app
    import backend.db as db
    with TestClient(app) as c:          # triggers startup -> init_db()
        if db.count_admins() == 0:
            token = c.post("/setup", json={"username": "admin", "password": "changeme123"}).json()["token"]
        else:
            token = c.post("/login", json={"username": "admin", "password": "changeme123"}).json()["token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


@pytest.fixture()
def project(client):
    """A fresh project per test that asks for one."""
    import backend.db as db
    r = client.post("/projects", json={"name": "Test " + str(len(db.list_projects()) + 1)})
    return r.json()
