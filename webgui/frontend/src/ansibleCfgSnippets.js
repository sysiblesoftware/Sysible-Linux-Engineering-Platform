// Ready-made ansible.cfg building blocks for the IDE "Insert setting" palette,
// shown when editing a project's ansible.cfg. Each item's `yaml` (plain INI text,
// the field name is shared with the task palette) is a commented setting or a
// whole section at its Ansible default, inserted at the cursor — so a config can
// be assembled from the menu with sane defaults instead of hand-typed/remembered.
// Reference: https://docs.ansible.com/ansible/latest/reference_appendices/config.html
const t = (name, search, yaml) => ({ name, search, yaml })

export const SNIPPET_GROUPS = [
  {
    group: 'Sections',
    items: [
      t('[defaults] section', 'defaults section general', `[defaults]\nforks = 10\nhost_key_checking = False\ntimeout = 30\ngathering = smart\nstdout_callback = default\nretry_files_enabled = False\n`),
      t('[privilege_escalation] section', 'privilege escalation become sudo section', `[privilege_escalation]\nbecome = False\nbecome_method = sudo\nbecome_user = root\nbecome_ask_pass = False\n`),
      t('[ssh_connection] section', 'ssh connection section', `[ssh_connection]\npipelining = True\nssh_args = -o ControlMaster=auto -o ControlPersist=60s\n`),
      t('[inventory] section', 'inventory plugins section', `[inventory]\nenable_plugins = host_list, script, auto, yaml, ini, toml\n`),
      t('[galaxy] section', 'galaxy collection server section', `[galaxy]\nignore_certs = False\n`),
    ],
  },
  {
    group: 'General [defaults]',
    items: [
      t('forks (parallelism)', 'forks parallel workers concurrency', `# Number of hosts to configure in parallel.\nforks = 10\n`),
      t('host_key_checking', 'host key checking known_hosts ssh', `# Verify SSH host keys against known_hosts (SLEP leaves this off for first runs).\nhost_key_checking = False\n`),
      t('timeout (connect seconds)', 'timeout connection seconds', `# Seconds to wait for the SSH connection to establish.\ntimeout = 30\n`),
      t('remote_user', 'remote user ssh login default', `# Default user to connect as when a host/credential doesn't set one.\nremote_user = root\n`),
      t('gathering', 'gathering facts smart explicit implicit', `# Gather host facts before the play: smart | explicit | implicit.\ngathering = smart\n`),
      t('inventory (default path)', 'inventory default file path', `# Default inventory this project uses when a run doesn't pass one.\ninventory = ./inventory\n`),
      t('interpreter_python', 'python interpreter discovery', `# How Ansible picks the target Python interpreter.\ninterpreter_python = auto_silent\n`),
    ],
  },
  {
    group: 'Output & logging',
    items: [
      t('stdout_callback', 'stdout callback output format yaml', `# Play output style. 'yaml' gives readable, multi-line task results.\nstdout_callback = default\n`),
      t('callbacks_enabled', 'callbacks enabled profile timer', `# Extra callback plugins to load (e.g. timing/profiling).\ncallbacks_enabled = profile_tasks\n`),
      t('log_path', 'log path file logging', `# Write a full run log to this file.\nlog_path = ./ansible.log\n`),
      t('retry_files_enabled', 'retry files disable', `# Don't scatter *.retry files through the project.\nretry_files_enabled = False\n`),
      t('deprecation_warnings', 'deprecation warnings silence', `# Show deprecation warnings during runs.\ndeprecation_warnings = True\n`),
      t('no_target_syslog', 'no target syslog no_log', `# Keep task arguments out of the target's syslog.\nno_target_syslog = True\n`),
    ],
  },
  {
    group: 'Roles & collections',
    items: [
      t('roles_path', 'roles path search', `# Where this project keeps its roles (relative to the project).\nroles_path = ./roles\n`),
      t('collections_path', 'collections path search galaxy', `# Where this project keeps its installed collections.\ncollections_path = ./collections\n`),
      t('library (custom modules)', 'library module path custom', `# Extra path for custom modules shipped with this project.\nlibrary = ./library\n`),
      t('filter_plugins', 'filter plugins jinja custom', `# Extra path for custom Jinja filter plugins.\nfilter_plugins = ./filter_plugins\n`),
    ],
  },
  {
    group: 'Privilege escalation',
    items: [
      t('become', 'become sudo escalate root', `# Escalate to root by default? Plays/tasks can still override.\nbecome = False\n`),
      t('become_method', 'become method sudo su doas', `# How to escalate: sudo | su | doas | pbrun | …\nbecome_method = sudo\n`),
      t('become_user', 'become user target root', `# Which user to become.\nbecome_user = root\n`),
      t('become_ask_pass', 'become ask pass prompt password', `# Prompt for the privilege-escalation password.\nbecome_ask_pass = False\n`),
    ],
  },
  {
    group: 'SSH connection',
    items: [
      t('pipelining', 'pipelining speed ssh performance', `# Reuse the SSH connection across tasks (needs target sudoers without requiretty).\npipelining = True\n`),
      t('ssh_args', 'ssh args controlmaster controlpersist', `# Extra SSH args. SLEP adds its own bastion ProxyCommand per host — these combine.\nssh_args = -o ControlMaster=auto -o ControlPersist=60s\n`),
      t('control_path_dir', 'control path socket dir', `# Directory for the SSH control sockets.\ncontrol_path_dir = ~/.ansible/cp\n`),
      t('retries (connection)', 'retries reconnect attempts', `# How many times to retry a failed SSH connection.\nretries = 3\n`),
      t('scp_if_ssh', 'scp sftp transfer method', `# File transfer method: smart | True (scp) | False (sftp).\nscp_if_ssh = smart\n`),
    ],
  },
]
