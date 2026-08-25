// Ready-made Salt state (SLS) snippets for the IDE "Insert state" palette. Each
// item's `yaml` is a complete state declaration inserted at the cursor. Grouped to
// MIRROR the Ansible task library one-for-one so the two engines feel the same —
// same group names, same coverage; `search` also matches on keywords/state module
// names. States are agentless here — SLEP runs them with salt-ssh — so everything
// below works over SSH with no minion.
const t = (name, search, yaml) => ({ name, search, yaml })

export const SNIPPET_GROUPS = [
  {
    group: 'Basics',
    items: [
      t('Run a command', 'cmd.run shell command', `run_hello:\n  cmd.run:\n    - name: echo "hello from SLEP"\n`),
      t('Run once (creates/unless)', 'cmd.run unless idempotent creates', `bootstrap_once:\n  cmd.run:\n    - name: /usr/local/bin/bootstrap.sh\n    - creates: /var/lib/bootstrapped\n`),
      t('Show a message (test)', 'debug print test show_notification', `show_message:\n  test.show_notification:\n    - text: "value applied by SLEP"\n`),
      t('No-op / succeed', 'test.succeed_without_changes noop ping', `ok:\n  test.succeed_without_changes:\n    - name: reachable\n`),
      t('Gather grains (facts)', 'grains facts setup', `# Grains are Salt's facts. Reference them inline, e.g. {{ grains['os_family'] }}\n# or dump them:  salt-ssh '*' grains.items\nshow_os:\n  test.show_notification:\n    - text: "os_family is {{ grains['os_family'] }}"\n`),
      t('Include another state', 'include import', `include:\n  - common\n  - webserver\n`),
      t('Require / dependency', 'require order dependency', `start_app:\n  service.running:\n    - name: app\n    - require:\n      - pkg: install_app\n`),
    ],
  },
  {
    group: 'Users & Groups',
    items: [
      t('Create a user', 'user.present account add', `create_user:\n  user.present:\n    - name: deploy\n    - shell: /bin/bash\n    - home: /home/deploy\n    - groups:\n      - sudo\n`),
      t('Set / unlock password', 'password lock passwd hash', `set_password:\n  user.present:\n    - name: deploy\n    # sha512 crypt hash (e.g. from: openssl passwd -6)\n    - password: "$6$rounds=656000$examplehash"\n`),
      t('Create a group', 'group.present', `create_group:\n  group.present:\n    - name: engineering\n`),
      t('Sudo access (drop-in)', 'sudo sudoers wheel', `engineering_sudo:\n  file.managed:\n    - name: /etc/sudoers.d/engineering\n    - contents: "%engineering ALL=(ALL) NOPASSWD:ALL"\n    - mode: "0440"\n    - check_cmd: visudo -cf\n`),
      t('Authorized SSH key', 'ssh_auth authorized key', `deploy_key:\n  ssh_auth.present:\n    - user: deploy\n    - source: salt://files/id_ed25519.pub\n`),
    ],
  },
  {
    group: 'Packages & Repositories',
    items: [
      t('Install a package', 'pkg.installed install', `install_package:\n  pkg.installed:\n    - name: your-package\n`),
      t('Install several packages', 'pkg.installed list multiple', `install_tools:\n  pkg.installed:\n    - pkgs:\n      - git\n      - curl\n      - htop\n`),
      t('Keep package at latest', 'pkg.latest upgrade', `keep_updated:\n  pkg.latest:\n    - name: your-package\n`),
      t('Update all packages', 'pkg.uptodate upgrade patch dist', `upgrade_all:\n  pkg.uptodate:\n    - refresh: True\n`),
      t('Add an apt repository', 'pkgrepo apt repo ppa', `add_apt_repo:\n  pkgrepo.managed:\n    - humanname: Example repo\n    - name: deb https://example.com/apt stable main\n    - file: /etc/apt/sources.list.d/example.list\n`),
      t('Add a yum/dnf repository', 'pkgrepo yum dnf repo', `add_yum_repo:\n  pkgrepo.managed:\n    - name: example\n    - humanname: Example repo\n    - baseurl: https://example.com/rpm/\n    - gpgcheck: 0\n`),
    ],
  },
  {
    group: 'Services & Systemd',
    items: [
      t('Service running + enabled', 'service.running enable start', `run_service:\n  service.running:\n    - name: your-service\n    - enable: True\n`),
      t('Restart on config change (watch)', 'watch restart reload service', `reload_on_change:\n  service.running:\n    - name: your-service\n    - watch:\n      - file: /etc/app/app.conf\n`),
      t('Install a systemd unit', 'systemd unit service file', `myapp_unit:\n  file.managed:\n    - name: /etc/systemd/system/myapp.service\n    - mode: "0644"\n    - contents: |\n        [Unit]\n        Description=My App\n        [Service]\n        ExecStart=/usr/local/bin/myapp\n        [Install]\n        WantedBy=multi-user.target\nmyapp_reload:\n  module.run:\n    - name: service.systemctl_reload\n    - onchanges:\n      - file: myapp_unit\n`),
      t('Reload systemd daemon', 'daemon-reload systemctl', `reload_systemd:\n  module.run:\n    - name: service.systemctl_reload\n`),
      t('Systemd timer', 'timer cron systemd schedule', `enable_timer:\n  service.running:\n    - name: mytask.timer\n    - enable: True\n`),
    ],
  },
  {
    group: 'Remove & teardown',
    items: [
      t('Remove a package', 'remove uninstall pkg.removed absent', `remove_package:\n  pkg.removed:\n    - name: your-package\n`),
      t('Purge a package (apt)', 'purge remove pkg.purged apt debian', `purge_package:\n  pkg.purged:\n    - name: your-package\n`),
      t('Stop & disable a service', 'service.dead stop disable', `stop_service:\n  service.dead:\n    - name: your-service\n    - enable: False\n`),
      t('Mask a service', 'service.masked mask block', `mask_service:\n  service.masked:\n    - name: your-service\n`),
      t('Remove a systemd unit', 'remove delete systemd unit file', `remove_unit:\n  file.absent:\n    - name: /etc/systemd/system/myapp.service\nremove_unit_reload:\n  module.run:\n    - name: service.systemctl_reload\n    - onchanges:\n      - file: remove_unit\n`),
      t('Delete a file or directory', 'file.absent remove delete rm', `remove_path:\n  file.absent:\n    - name: /etc/app/old.conf\n`),
      t('Remove a user', 'user.absent remove delete userdel', `remove_user:\n  user.absent:\n    - name: olduser\n    - purge: True\n`),
      t('Remove a group', 'group.absent remove delete', `remove_group:\n  group.absent:\n    - name: oldgroup\n`),
      t('Remove a cron job', 'cron.absent remove delete crontab', `remove_cron:\n  cron.absent:\n    - name: /usr/local/bin/backup.sh\n    - user: root\n`),
      t('Remove an authorized SSH key', 'ssh_auth.absent remove key', `remove_key:\n  ssh_auth.absent:\n    - user: deploy\n    - source: salt://files/id_ed25519.pub\n`),
      t('Unmount & drop from fstab', 'mount.unmounted umount fstab', `drop_mount:\n  mount.unmounted:\n    - name: /data\n    - persist: True\n`),
    ],
  },
  {
    group: 'Cron & Environment',
    items: [
      t('Cron job', 'cron.present schedule crontab', `nightly_backup:\n  cron.present:\n    - name: /usr/local/bin/backup.sh\n    - user: root\n    - minute: "0"\n    - hour: "2"\n`),
      t('Environment variable (system-wide)', 'env variable profile.d shell', `system_env:\n  file.managed:\n    - name: /etc/profile.d/myenv.sh\n    - contents: "export MYVAR=value"\n    - mode: "0644"\n`),
      t('Shell alias (system-wide)', 'alias shell profile', `system_alias:\n  file.managed:\n    - name: /etc/profile.d/aliases.sh\n    - contents: "alias ll='ls -alF'"\n    - mode: "0644"\n`),
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
      t('Set an ACL', 'acl permissions setfacl', `set_acl:\n  cmd.run:\n    - name: setfacl -m u:deploy:rwx /srv/share\n    - unless: getfacl /srv/share | grep -q "user:deploy:rwx"\n`),
    ],
  },
  {
    group: 'Storage & LVM',
    items: [
      t('LVM volume group', 'lvm.vg_present volume group pv', `data_vg:\n  lvm.vg_present:\n    - name: data_vg\n    - devices: /dev/sdb\n`),
      t('LVM logical volume', 'lvm.lv_present logical volume', `data_lv:\n  lvm.lv_present:\n    - name: data_lv\n    - vgname: data_vg\n    - size: 20G\n`),
      t('Create a filesystem', 'blockdev.formatted mkfs xfs ext4', `make_fs:\n  blockdev.formatted:\n    - name: /dev/data_vg/data_lv\n    - fs_type: xfs\n`),
      t('Partition a disk (parted)', 'partition parted disk', `create_partition:\n  cmd.run:\n    - name: parted -s /dev/sdb mklabel gpt mkpart primary 0% 100%\n    - unless: parted -s /dev/sdb print | grep -q "primary"\n`),
      t('Mount & persist', 'mount.mounted fstab', `data_mount:\n  mount.mounted:\n    - name: /data\n    - device: /dev/data_vg/data_lv\n    - fstype: xfs\n    - persist: True\n`),
      t('LVM snapshot', 'lvm snapshot backup', `data_snap:\n  lvm.lv_present:\n    - name: data_snap\n    - vgname: data_vg\n    - snapshot: data_lv\n    - size: 5G\n`),
    ],
  },
  {
    group: 'Networking',
    items: [
      t('Set hostname', 'hostname network.system', `set_hostname:\n  cmd.run:\n    - name: hostnamectl set-hostname web01\n    - unless: test "$(hostname)" = "web01"\n`),
      t('Configure a static IP (nmcli)', 'network ip nmcli static', `static_ip:\n  cmd.run:\n    - name: >\n        nmcli con mod eth0 ipv4.addresses 192.168.1.50/24\n        ipv4.gateway 192.168.1.1 ipv4.dns 1.1.1.1 ipv4.method manual &&\n        nmcli con up eth0\n    - onlyif: nmcli -t -f NAME con show | grep -q '^eth0$'\n`),
      t('/etc/hosts entry', 'host.present dns hostfile', `db_host:\n  host.present:\n    - name: db01\n    - ip: 192.168.1.60\n`),
      t('Kernel parameter (sysctl)', 'sysctl.present kernel tuning', `ip_forward:\n  sysctl.present:\n    - name: net.ipv4.ip_forward\n    - value: 1\n`),
    ],
  },
  {
    group: 'Firewall',
    items: [
      t('Open a port (firewalld)', 'firewall firewalld port', `open_port:\n  firewalld.present:\n    - name: public\n    - ports:\n      - 8080/tcp\n`),
      t('Allow a service (firewalld)', 'firewall firewalld service', `allow_https:\n  firewalld.present:\n    - name: public\n    - services:\n      - https\n`),
      t('Allow a port (ufw)', 'firewall ufw ubuntu', `ufw_allow_ssh:\n  cmd.run:\n    - name: ufw allow 22/tcp\n    - unless: ufw status | grep -q "22/tcp"\n`),
    ],
  },
  {
    group: 'Security',
    items: [
      t('SELinux mode', 'selinux enforcing security', `selinux_enforcing:\n  selinux.mode:\n    - name: enforcing\n`),
      t('Harden SSH (no root login)', 'ssh hardening sshd security', `disable_root_ssh:\n  file.replace:\n    - name: /etc/ssh/sshd_config\n    - pattern: '^#?PermitRootLogin.*'\n    - repl: 'PermitRootLogin no'\n    - append_if_not_found: True\nreload_sshd:\n  service.running:\n    - name: sshd\n    - watch:\n      - file: disable_root_ssh\n`),
      t('Password policy (pwquality)', 'password policy pam pwquality', `min_password_len:\n  file.replace:\n    - name: /etc/security/pwquality.conf\n    - pattern: '^#?minlen.*'\n    - repl: 'minlen = 14'\n    - append_if_not_found: True\n`),
      t('Install security updates', 'security updates patch', `security_updates:\n  pkg.uptodate:\n    - refresh: True\n`),
    ],
  },
  {
    group: 'Time & Certificates',
    items: [
      t('Set timezone', 'timezone.system time clock', `set_tz:\n  timezone.system:\n    - name: America/New_York\n`),
      t('NTP / chrony', 'ntp chrony time sync', `chrony:\n  pkg.installed:\n    - name: chrony\n  service.running:\n    - name: chronyd\n    - enable: True\n    - require:\n      - pkg: chrony\n`),
      t('Private key + self-signed cert', 'certificate tls ssl x509', `app_key:\n  x509.private_key_managed:\n    - name: /etc/ssl/private/app.key\n    - bits: 2048\napp_cert:\n  x509.certificate_managed:\n    - name: /etc/ssl/certs/app.crt\n    - signing_private_key: /etc/ssl/private/app.key\n    - CN: app.example.com\n    - days_valid: 365\n    - require:\n      - x509: app_key\n`),
    ],
  },
  {
    group: 'Containers & Directory Services',
    items: [
      t('Docker container', 'docker container run', `web_container:\n  docker_container.running:\n    - name: web\n    - image: nginx:latest\n    - port_bindings:\n      - 8080:80\n`),
      t('Podman container', 'podman container run', `podman_web:\n  cmd.run:\n    - name: podman run -d --name web -p 8080:80 docker.io/library/nginx:latest\n    - unless: podman ps --format '{{.Names}}' | grep -q '^web$'\n`),
      t('Join Active Directory (realm)', 'active directory realm ad sssd join', `join_ad:\n  cmd.run:\n    - name: realm join --user={{ pillar['ad_user'] }} example.com\n    - creates: /etc/sssd/sssd.conf\n`),
    ],
  },
  {
    group: 'Backup & Maintenance',
    items: [
      t('Create an archive', 'backup archive tar compress', `backup_app:\n  archive.tar:\n    - name: /backups/app.tgz\n    - sources:\n      - /etc/app\n    - options: czf\n`),
      t('Extract an archive', 'archive.extracted tar unzip', `unpack_app:\n  archive.extracted:\n    - name: /opt/app\n    - source: salt://files/app.tar.gz\n    - enforce_toplevel: False\n`),
      t('Rsync a directory', 'backup rsync sync copy', `sync_data:\n  cmd.run:\n    - name: rsync -a --delete /data/ /backups/data/\n`),
      t('Reboot if required', 'cmd.run reboot restart', `reboot_if_needed:\n  cmd.run:\n    - name: shutdown -r +1 "SLEP maintenance reboot"\n    - onlyif: test -f /var/run/reboot-required\n`),
      t('OS release upgrade (assess)', 'upgrade release do-release-upgrade leapp', `check_release_upgrade:\n  cmd.run:\n    - name: do-release-upgrade -c\n    - onlyif: which do-release-upgrade\n`),
      t('Git checkout', 'git.latest clone repo source', `checkout_app:\n  git.latest:\n    - name: https://example.com/app.git\n    - target: /opt/app\n    - rev: main\n`),
    ],
  },
]
