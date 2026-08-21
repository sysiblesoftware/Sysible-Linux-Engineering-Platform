"""Git ops on a project working dir — init, commit, log, branch, status."""


def test_git_lifecycle(client, project):
    pid = project["id"]
    # not a repo yet
    assert client.get(f"/projects/{pid}/git/status").json()["repo"] is False
    # init
    st = client.post(f"/projects/{pid}/git/init").json()
    assert st["repo"] is True and st["branch"] == "main"
    # a file to commit
    client.put(f"/projects/{pid}/file", json={"path": "site.yml", "content": "- hosts: all\n"})
    status = client.get(f"/projects/{pid}/git/status").json()
    assert any(f["path"] == "site.yml" for f in status["files"])
    # commit needs a message
    assert client.post(f"/projects/{pid}/git/commit", json={"message": ""}).status_code == 400
    c = client.post(f"/projects/{pid}/git/commit", json={"message": "initial"})
    assert c.status_code == 200 and c.json()["ok"] is True
    # clean tree, one commit in the log
    assert client.get(f"/projects/{pid}/git/status").json()["files"] == []
    commits = client.get(f"/projects/{pid}/git/log").json()["commits"]
    assert commits and commits[0]["subject"] == "initial"
    # branch + checkout
    client.post(f"/projects/{pid}/git/checkout", json={"branch": "feature", "create": True})
    assert client.get(f"/projects/{pid}/git/status").json()["branch"] == "feature"
    assert set(client.get(f"/projects/{pid}/git/branches").json()["branches"]) == {"main", "feature"}


def test_git_token_stored_encrypted_not_leaked(client, project):
    import backend.db as db
    import backend.vault as vault
    pid = project["id"]
    client.post(f"/projects/{pid}/git/init")
    client.post(f"/projects/{pid}/git/remote",
                json={"url": "https://example.com/acme/repo.git", "token": "s3cr3t"})
    # status flags a token is set but never returns it
    st = client.get(f"/projects/{pid}/git/status").json()
    assert st["remote"] == "https://example.com/acme/repo.git" and st["has_token"] is True
    # the projects listing never carries the token
    proj = [p for p in client.get("/projects").json()["projects"] if p["id"] == pid][0]
    assert "git_token" not in proj and proj["has_git_token"] is True
    # stored encrypted, decrypts back
    full = db.get_project(pid, include_token=True)
    assert full["git_token"] and full["git_token"] != "s3cr3t"
    assert vault.decrypt(full["git_token"]) == "s3cr3t"


def test_push_without_remote_400(client, project):
    pid = project["id"]
    client.post(f"/projects/{pid}/git/init")
    assert client.post(f"/projects/{pid}/git/push").status_code == 400


def test_create_project_from_clone(client, monkeypatch):
    import backend.gitops as g
    called = {}
    monkeypatch.setattr(g, "clone", lambda pid, url, token="": called.update(pid=pid, url=url, token=token) or {"repo": True})
    r = client.post("/projects", json={"name": "cloned", "clone_url": "https://example.com/a/b.git", "git_token": "tok"})
    assert r.status_code == 200
    assert called["url"] == "https://example.com/a/b.git" and called["token"] == "tok"


def test_create_project_from_clone_failure_rolls_back(client, monkeypatch):
    import backend.gitops as g, backend.db as db
    def boom(pid, url, token=""):
        raise g.GitError("auth failed")
    monkeypatch.setattr(g, "clone", boom)
    before = len(db.list_projects())
    r = client.post("/projects", json={"name": "willfail", "clone_url": "https://x/y.git"})
    assert r.status_code == 400 and "Clone failed" in r.json()["detail"]
    assert len(db.list_projects()) == before   # project rolled back
