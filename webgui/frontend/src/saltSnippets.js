// Ready-made Salt state (SLS) snippets for the IDE "Insert state" palette. Each
// item's `yaml` is a complete state declaration inserted at the cursor. Grouped
// to mirror the Ansible task library so the engines feel the same; `search` also
// matches on keywords/state module names. States are agentless here — SLEP runs
// them with salt-ssh — so everything below works over SSH with no minion.
const t = (name, search, yaml) => ({ name, search, yaml })

export const SNIPPET_GROUPS = [
  {
    group: 'Basics',
    items: [
      t('Run a command', 'cmd.run shell command', `run_hello:\n  cmd.run:\n    - name: echo "hello from SLEP"\n`),
      t('Run once (unless)', 'cmd.run unless idempotent creates', `bootstrap_once:\n  cmd.run:\n    - name: /usr/local/bin/bootstrap.sh\n    - creates: /var/lib/bootstrapped\n`),
      t('Include another state', 'include import', `include:\n  - common\n  - webserver\n`),
      t('Require / dependency', 'require order dependency', `start_app:\n  service.running:\n    - name: app\n    - require:\n      - pkg: install_app\n`),
    ],
  },
  {
    group: 'Packages & Repositories',
    items: [
      t('Install a package', 'pkg.installed install', `install_package:\n  pkg.installed:\n    - name: your-package\n`),
      t('Install several packages', 'pkg.installed list multiple', `install_tools:\n  pkg.installed:\n    - pkgs:\n      - git\n      - curl\n      - htop\n`),
      t('Keep package at latest', 'pkg.latest upgrade', `keep_updated:\n  pkg.latest:\n    - name: your-package\n`),
      t('Add an apt repository', 'pkgrepo apt repo ppa', `add_repo:\n  pkgrepo.managed:\n    - humanname: Example repo\n    - name: deb https://example.com/apt stable main\n    - file: /etc/apt/sources.list.d/example.list\n`),
    ],
  },
  {
    group: 'Services & Systemd',
    items: [
      t('Service running + enabled', 'service.running enable start', `run_service:\n  service.running:\n    - name: your-service\n    - enable: True\n`),
      t('Service stopped + disabled', 'service.dead stop disable', `stop_service:\n  service.dead:\n    - name: your-service\n    - enable: False\n`),
      t('Restart on config change (watch)', 'watch restart reload service', `reload_on_change:\n  service.running:\n    - name: your-service\n    - watch:\n      - file: /etc/app/app.conf\n`),
    ],
  },
  {
    group: 'Users & Groups',
    items: [
      t('Create a user', 'user.present account add', `create_user:\n  user.present:\n    - name: deploy\n    - shell: /bin/bash\n    - home: /home/deploy\n    - groups:\n      - sudo\n`),
      t('Create a group', 'group.present', `create_group:\n  group.present:\n    - name: engineering\n`),
      t('Authorized SSH key', 'ssh_auth authorized key', `deploy_key:\n  ssh_auth.present:\n    - user: deploy\n    - source: salt://files/id_ed25519.pub\n`),
    ],
  },
  {
    group: 'Files & Permissions',
    items: [
      t('Manage a file', 'file.managed copy content', `app_config:\n  file.managed:\n    - name: /etc/app/app.conf\n    - source: salt://files/app.conf\n    - user: root\n    - group: root\n    - mode: "0644"\n`),
      t('Manage a file (template)', 'file.managed jinja template', `rendered_config:\n  file.managed:\n    - name: /etc/app/app.conf\n    - source: salt://templates/app.conf.jinja\n    - template: jinja\n    - context:\n        port: 8080\n`),
      t('Directory with permissions', 'file.directory mkdir chmod', `app_dir:\n  file.directory:\n    - name: /opt/app\n    - user: deploy\n    - group: deploy\n    - mode: "0755"\n    - makedirs: True\n`),
      t('Ensure a line in a file', 'file.line lineinfile edit', `enable_forwarding:\n  file.line:\n    - name: /etc/sysctl.conf\n    - content: "net.ipv4.ip_forward = 1"\n    - mode: ensure\n    - match: "net.ipv4.ip_forward"\n`),
      t('Symlink', 'file.symlink link', `link_current:\n  file.symlink:\n    - name: /opt/app/current\n    - target: /opt/app/releases/v1\n`),
    ],
  },
  {
    group: 'Storage & Mounts',
    items: [
      t('Mount & persist a filesystem', 'mount.mounted fstab', `data_mount:\n  mount.mounted:\n    - name: /data\n    - device: /dev/data_vg/data_lv\n    - fstype: xfs\n    - persist: True\n`),
      t('LVM volume group', 'lvm.vg_present volume group', `data_vg:\n  lvm.vg_present:\n    - name: data_vg\n    - devices: /dev/sdb\n`),
      t('LVM logical volume', 'lvm.lv_present logical volume', `data_lv:\n  lvm.lv_present:\n    - name: data_lv\n    - vgname: data_vg\n    - size: 20G\n`),
    ],
  },
  {
    group: 'Networking & Time',
    items: [
      t('/etc/hosts entry', 'host.present dns hostfile', `db_host:\n  host.present:\n    - name: db01\n    - ip: 192.168.1.60\n`),
      t('Set a kernel parameter (sysctl)', 'sysctl.present kernel', `ip_forward:\n  sysctl.present:\n    - name: net.ipv4.ip_forward\n    - value: 1\n`),
      t('Set timezone', 'timezone.system time clock', `set_tz:\n  timezone.system:\n    - name: America/New_York\n`),
      t('NTP / chrony service', 'ntp chrony time sync', `chrony:\n  pkg.installed:\n    - name: chrony\n  service.running:\n    - name: chronyd\n    - enable: True\n    - require:\n      - pkg: chrony\n`),
    ],
  },
  {
    group: 'Scheduling & Maintenance',
    items: [
      t('Cron job', 'cron.present schedule crontab', `nightly_backup:\n  cron.present:\n    - name: /usr/local/bin/backup.sh\n    - user: root\n    - minute: "0"\n    - hour: "2"\n`),
      t('Extract an archive', 'archive.extracted tar unzip', `unpack_app:\n  archive.extracted:\n    - name: /opt/app\n    - source: salt://files/app.tar.gz\n    - enforce_toplevel: False\n`),
      t('Git checkout', 'git.latest clone repo', `checkout_app:\n  git.latest:\n    - name: https://example.com/app.git\n    - target: /opt/app\n    - rev: main\n`),
      t('Reboot if required', 'cmd.run reboot restart', `reboot_if_needed:\n  cmd.run:\n    - name: shutdown -r +1 "SLEP maintenance reboot"\n    - onlyif: test -f /var/run/reboot-required\n`),
    ],
  },
]
