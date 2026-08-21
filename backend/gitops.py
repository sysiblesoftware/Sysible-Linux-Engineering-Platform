"""Git operations on a project's working directory.

SLEP projects are plain directories of IaC files; this gives them full version
control from the console — init, status, stage/commit, branch/checkout, log,
diff, and push/pull to a remote. Everything runs `git` inside the project dir
(db.project_dir), so it's the same repo you'd get on a shell.

Auth for push/pull is an optional per-project token, stored encrypted (via the
vault) and never returned to the browser. At push/pull time it's written to a
0600 credential file and handed to git via `credential.helper=store --file=…`
so the token never lands in argv, the process list, or the repo's config.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

from . import db, vault

_IDENTITY = ["-c", "user.name=Sysible SLEP", "-c", "user.email=slep@sysible.local"]


class GitError(Exception):
    pass


def _run(pid: int, args, extra_cfg=None, env=None, timeout=180):
    cwd = str(db.project_dir(pid).resolve())
    cmd = ["git", *_IDENTITY, *(extra_cfg or []), *args]
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError("git command timed out.")
    except FileNotFoundError:
        raise GitError("git is not installed in this image.")


def is_repo(pid: int) -> bool:
    return (db.project_dir(pid) / ".git").exists()


def init(pid: int):
    _run(pid, ["init"])
    _run(pid, ["symbolic-ref", "HEAD", "refs/heads/main"])   # default branch: main
    return status(pid)


def status(pid: int):
    if not is_repo(pid):
        return {"repo": False}
    # symbolic-ref reports the branch even on an unborn branch (before 1st commit);
    # rev-parse would just say "HEAD" there. Fall back for detached HEAD.
    branch = _run(pid, ["symbolic-ref", "--short", "HEAD"]).stdout.strip() \
        or _run(pid, ["rev-parse", "--short", "HEAD"]).stdout.strip() or "(no commits yet)"
    files = []
    for line in _run(pid, ["status", "--porcelain=v1"]).stdout.splitlines():
        if len(line) < 4:
            continue
        x, y, path = line[0], line[1], line[3:]
        files.append({"path": path, "x": x, "y": y,
                      "staged": x not in " ?", "untracked": x == "?"})
    remote = _run(pid, ["remote", "get-url", "origin"]).stdout.strip()
    ahead = behind = 0
    ab = _run(pid, ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
    if ab.returncode == 0 and ab.stdout.strip():
        parts = ab.stdout.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    proj = db.get_project(pid) or {}
    return {"repo": True, "branch": branch, "files": files, "remote": remote,
            "ahead": ahead, "behind": behind, "has_token": bool(proj.get("has_git_token"))}


def commit(pid: int, message: str, paths=None):
    if not (message or "").strip():
        raise GitError("A commit message is required.")
    if paths:
        _run(pid, ["add", "--", *paths])
    else:
        _run(pid, ["add", "-A"])
    r = _run(pid, ["commit", "-m", message])
    ok = r.returncode == 0
    if not ok and "nothing to commit" in (r.stdout + r.stderr):
        raise GitError("Nothing to commit — no staged changes.")
    if not ok:
        raise GitError((r.stderr or r.stdout).strip() or "commit failed")
    return {"ok": True, "output": (r.stdout + r.stderr).strip()}


def log(pid: int, n: int = 25):
    if not is_repo(pid):
        return []
    r = _run(pid, ["log", f"-{int(n)}", "--pretty=%h%x1f%an%x1f%ar%x1f%s"])
    out = []
    for line in r.stdout.splitlines():
        parts = (line.split("\x1f") + ["", "", "", ""])[:4]
        out.append({"hash": parts[0], "author": parts[1], "when": parts[2], "subject": parts[3]})
    return out


def diff(pid: int, path=None, staged=False):
    args = ["diff"] + (["--cached"] if staged else []) + (["--", path] if path else [])
    return _run(pid, args).stdout


def branches(pid: int):
    if not is_repo(pid):
        return []
    r = _run(pid, ["branch", "--format=%(refname:short)"])
    return [b.strip() for b in r.stdout.splitlines() if b.strip()]


def checkout(pid: int, branch: str, create: bool = False):
    branch = (branch or "").strip()
    if not branch:
        raise GitError("A branch name is required.")
    args = ["checkout"] + (["-b"] if create else []) + [branch]
    r = _run(pid, args)
    if r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip() or "checkout failed")
    return {"ok": True, "output": (r.stdout + r.stderr).strip()}


def set_remote(pid: int, url: str):
    url = (url or "").strip()
    if _run(pid, ["remote"]).stdout.strip():
        _run(pid, ["remote", "set-url", "origin", url] if url else ["remote", "remove", "origin"])
    elif url:
        _run(pid, ["remote", "add", "origin", url])
    db.set_project_scm(pid, scm_url=url)


def _clean_host_url(url: str) -> str:
    """Strip any embedded credentials from an https URL (keep host+path)."""
    p = urlsplit(url)
    netloc = p.hostname or ""
    if p.port:
        netloc += f":{p.port}"
    return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def _token_cfg(url: str, token: str):
    """Return (extra_cfg, cleanup) that authenticates an https git op with `token`
    via a 0600 credential file — token never touches argv or config."""
    if not token or not url.startswith("https://"):
        return [], (lambda: None)
    p = urlsplit(_clean_host_url(url))
    origin = f"https://x-access-token:{token}@{p.hostname}{(':' + str(p.port)) if p.port else ''}"
    fd, path = tempfile.mkstemp(prefix="slep-git-")
    with os.fdopen(fd, "w") as f:
        f.write(origin + "\n")
    os.chmod(path, 0o600)
    return ["-c", f"credential.helper=store --file={path}"], (lambda: os.unlink(path))


def _remote_op(pid: int, op: str):
    st = status(pid)
    if not st["repo"]:
        raise GitError("This project is not a git repo yet — initialize it first.")
    url = st["remote"]
    if not url:
        raise GitError("No remote set. Add a remote URL first.")
    proj = db.get_project(pid, include_token=True) or {}
    token = vault.decrypt(proj["git_token"]) if proj.get("git_token") else ""
    cfg, cleanup = _token_cfg(url, token)
    try:
        if op == "push":
            args = ["push", "--set-upstream", "origin", f"HEAD:{st['branch']}"]
        else:
            args = ["pull", "--no-edit", "origin", st["branch"]]
        r = _run(pid, args, extra_cfg=cfg)
    finally:
        cleanup()
    ok = r.returncode == 0
    out = (r.stdout + r.stderr).strip()
    # Never echo the token back even if git embedded it in an error.
    if token:
        out = out.replace(token, "***")
    if not ok:
        raise GitError(out or f"{op} failed")
    db.touch_project(pid)
    return {"ok": True, "output": out}


def push(pid: int):
    return _remote_op(pid, "push")


def pull(pid: int):
    return _remote_op(pid, "pull")
