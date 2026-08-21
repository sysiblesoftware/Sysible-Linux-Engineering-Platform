"""Per-project Ansible configuration — SLEP's first-class `ansible.cfg`.

Ansible already reads an `ansible.cfg` from the directory it runs in, and the
SLEP runner launches `ansible-playbook` from the project's own working dir. So
a project's configuration IS a real `ansible.cfg` on disk (git-committable,
portable, exactly what a stock Ansible user expects) — this module just gives
the console a first-class way to create, read, validate and write it, with a
sensible SLEP-tuned starter template.

Kept deliberately thin: the file lives at `<project>/ansible.cfg`, so the IDE,
git ops and the runner all see the same bytes.
"""
from __future__ import annotations

import configparser
import io
from pathlib import Path

from . import db

CONFIG_NAME = "ansible.cfg"

# A commented starter config covering the settings SLEP users reach for most.
# Values here mirror SLEP's built-in run defaults, so adopting the file changes
# nothing until you edit it — then your edits win.
DEFAULT_ANSIBLE_CFG = """\
# ansible.cfg — SLEP project configuration.
#
# Ansible reads this file from the project directory on every run (SLEP runs
# `ansible-playbook` from here), so everything below applies to all playbooks in
# this project. It is a normal ansible.cfg: commit it to git, edit it by hand.
# Full reference:
#   https://docs.ansible.com/ansible/latest/reference_appendices/config.html

[defaults]
# Number of hosts to configure in parallel.
forks = 10

# Verify SSH host keys against known_hosts. SLEP leaves this OFF for first-run
# friendliness; set to True once your hosts' keys are known and trusted.
host_key_checking = False

# Seconds to wait for the SSH connection to establish.
timeout = 30

# Gather host facts before the play runs: smart | explicit | implicit.
gathering = smart

# Where this project keeps its roles and collections (relative to the project).
roles_path = ./roles
collections_path = ./collections

# Play output style. Try 'yaml' for readable, multi-line task results.
stdout_callback = default

# Don't scatter *.retry files through the project.
retry_files_enabled = False

[privilege_escalation]
# Escalate to root (sudo) by default? Individual plays/tasks can still override.
# SLEP supplies the become password from the run credential when a task needs it.
become = False
become_method = sudo
become_user = root

[ssh_connection]
# Reuse the SSH connection across tasks — a big speedup on multi-task plays.
# (Requires the target's sudoers to NOT set 'requiretty'.)
pipelining = True

# Persist the SSH control socket between tasks. SLEP adds its own bastion
# ProxyCommand per host in the inventory; these args are combined, not replaced.
ssh_args = -o ControlMaster=auto -o ControlPersist=60s
"""


def config_path(pid: int) -> Path:
    return db.project_dir(pid) / CONFIG_NAME


def exists(pid: int) -> bool:
    return config_path(pid).is_file()


def read(pid: int) -> dict:
    """Return {exists, path, content}. content is "" when no file is present."""
    p = config_path(pid)
    return {
        "exists": p.is_file(),
        "path": CONFIG_NAME,
        "content": p.read_text() if p.is_file() else "",
    }


def validate(content: str) -> None:
    """Raise ValueError if the text isn't a parseable INI ansible.cfg. Ansible
    uses Python's configparser, so this catches the same errors a real run would
    (duplicate sections, malformed lines) before the file is saved."""
    parser = configparser.ConfigParser(strict=True)
    try:
        parser.read_file(io.StringIO(content))
    except configparser.Error as e:
        raise ValueError(str(e).replace("\n", " ").strip())


def write(pid: int, content: str) -> dict:
    """Validate and write `ansible.cfg`. Returns read()'s shape."""
    validate(content)
    config_path(pid).write_text(content)
    db.touch_project(pid)
    return read(pid)


def ensure_default(pid: int) -> dict:
    """Create the file from the template if it doesn't exist yet; return read()."""
    p = config_path(pid)
    if not p.is_file():
        p.write_text(DEFAULT_ANSIBLE_CFG)
        db.touch_project(pid)
    return read(pid)


def defines(pid: int, section: str, option: str) -> bool:
    """True if the project's ansible.cfg explicitly sets `[section] option`.
    The runner uses this to know when NOT to override a setting via the
    environment (env vars win over ansible.cfg, so a user's explicit choice
    would otherwise be silently ignored)."""
    p = config_path(pid)
    if not p.is_file():
        return False
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read_file(io.StringIO(p.read_text()))
    except configparser.Error:
        return False
    return parser.has_option(section, option)
