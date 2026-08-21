"""Terraform runner — OpenTofu tool selection + stale-lock self-heal signatures."""
import backend.runners.terraform_runner as tr


def test_resolve_tool_prefers_explicit_choice(monkeypatch):
    monkeypatch.setattr(tr.shutil, "which", lambda b: "/usr/bin/" + b)  # both present
    assert tr._resolve_tool("tofu") == "tofu"
    assert tr._resolve_tool("opentofu") == "tofu"
    assert tr._resolve_tool("terraform") == "terraform"
    assert tr._resolve_tool("tf") == "terraform"


def test_resolve_tool_env_default(monkeypatch):
    monkeypatch.setattr(tr.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setenv("SLEP_TF_TOOL", "tofu")
    assert tr._resolve_tool("") == "tofu"


def test_resolve_tool_falls_back_when_missing(monkeypatch):
    # Only tofu is installed → a 'terraform' request still runs (tofu is a drop-in).
    monkeypatch.setattr(tr.shutil, "which", lambda b: "/usr/bin/tofu" if b == "tofu" else None)
    monkeypatch.delenv("SLEP_TF_TOOL", raising=False)
    assert tr._resolve_tool("terraform") == "tofu"
    assert tr._resolve_tool("") == "tofu"
    # Only terraform installed → auto picks terraform.
    monkeypatch.setattr(tr.shutil, "which", lambda b: "/usr/bin/terraform" if b == "terraform" else None)
    assert tr._resolve_tool("") == "terraform"


def test_stash_and_pop_tool():
    tr.stash_tool(4321, "tofu")
    assert tr.pop_tool(4321) == "tofu"
    assert tr.pop_tool(4321) == ""       # consumed once
    tr.stash_tool(4322, "")              # blank is ignored
    assert tr.pop_tool(4322) == ""


def test_schema_mismatch_signatures_cover_the_reported_errors():
    # The exact strings from a stale-lock libvirt failure must trigger the -upgrade
    # self-heal (see the run log: "Unsupported argument", "no definition was found").
    log = 'Error: Unsupported argument\nAn argument named "pool" is not expected here.'
    assert any(s in log for s in tr._SCHEMA_MISMATCH)
    log2 = 'Error: Missing required argument\nThe argument "meta_data" is required, but no definition was found.'
    assert any(s in log2 for s in tr._SCHEMA_MISMATCH)
