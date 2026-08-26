// Ready-made Ansible task snippets for the IDE "Insert task" palette. Each item's
// `yaml` is a complete task block (fully-qualified module names) inserted at the
// cursor, indented for a play's `tasks:` list (task at 4 spaces). Grouped to
// mirror the Sysible Controller System Administration toolset; `search` also
// matches on keywords/module names. Modules from community.general / ansible.posix
// / community.crypto / community.docker / containers.podman ship in the full
// `ansible` package baked into the SLEP image.
const t = (name, search, yaml) => ({ name, search, yaml })

// Playbook-structure building blocks — the scaffolding a task list needs to
// become a runnable playbook (play header, targeting, vars, handlers, roles),
// plus the common task modifiers. Offered alongside the task library so a
// playbook can be assembled from the palette or right-click, not hand-typed.
// These sit at play/task level, so they're not indented for a `tasks:` list.
export const PLAY_GROUPS = [
  {
    group: 'Play & structure',
    items: [
      t('Play header (hosts + tasks)', 'play header hosts tasks header', `- name: Configure hosts\n  hosts: all\n  become: true\n  gather_facts: true\n  tasks:\n`),
      t('Full playbook skeleton', 'playbook skeleton scaffold full', `- name: Configure hosts\n  hosts: all\n  become: true\n  gather_facts: true\n  vars:\n    example_var: value\n  tasks:\n    - name: Ping the host\n      ansible.builtin.ping:\n  handlers:\n    - name: restart sshd\n      ansible.builtin.service:\n        name: sshd\n        state: restarted\n`),
      t('hosts: (targets)', 'hosts target group pattern', `  hosts: all\n`),
      t('become (privilege escalation)', 'become sudo root privilege', `  become: true\n  become_user: root\n`),
      t('gather_facts: false', 'gather facts speed', `  gather_facts: false\n`),
      t('serial (rolling batches)', 'serial rolling batch canary', `  serial: 2\n`),
      t('vars: block', 'vars variables', `  vars:\n    app_name: myapp\n    app_port: 8080\n`),
      t('vars_files', 'vars files include external', `  vars_files:\n    - vars/main.yml\n`),
      t('vars_prompt', 'vars prompt interactive input', `  vars_prompt:\n    - name: username\n      prompt: "Username?"\n      private: false\n`),
      t('handlers: section', 'handlers notify restart', `  handlers:\n    - name: restart service\n      ansible.builtin.service:\n        name: your-service\n        state: restarted\n`),
      t('roles: section', 'roles role', `  roles:\n    - common\n    - webserver\n`),
      t('pre_tasks / post_tasks', 'pre post tasks', `  pre_tasks:\n    - name: Update apt cache\n      ansible.builtin.apt:\n        update_cache: true\n      become: true\n  post_tasks:\n    - name: Notify done\n      ansible.builtin.debug:\n        msg: "Play complete"\n`),
    ],
  },
  {
    group: 'Task modifiers',
    items: [
      t('when (conditional)', 'when condition if', `      when: ansible_facts['os_family'] == 'Debian'\n`),
      t('loop', 'loop with_items iterate', `      loop:\n        - one\n        - two\n        - three\n`),
      t('loop over dicts', 'loop dict key value', `      loop: "{{ users }}"\n      loop_control:\n        label: "{{ item.name }}"\n`),
      t('register + debug', 'register capture output', `      register: result\n`),
      t('notify a handler', 'notify handler trigger', `      notify: restart service\n`),
      t('tags', 'tags tag selective', `      tags:\n        - config\n`),
      t('become (per task)', 'become task sudo', `      become: true\n`),
      t('delegate_to', 'delegate local run-on', `      delegate_to: localhost\n`),
      t('run_once', 'run once single', `      run_once: true\n`),
      t('ignore_errors', 'ignore errors continue', `      ignore_errors: true\n`),
      t('changed_when / failed_when', 'changed failed when idempotent', `      changed_when: false\n      failed_when: false\n`),
      t('block / rescue / always', 'block rescue always try', `    - name: Attempt with recovery\n      block:\n        - name: Try this\n          ansible.builtin.command: /usr/bin/false\n      rescue:\n        - name: On failure\n          ansible.builtin.debug:\n            msg: "recovering"\n      always:\n        - name: Always run\n          ansible.builtin.debug:\n            msg: "cleanup"\n`),
      t('import_tasks / include_tasks', 'import include tasks file', `    - name: Include task file\n      ansible.builtin.include_tasks: tasks/setup.yml\n`),
    ],
  },
]

export const SNIPPET_GROUPS = [
  {
    group: 'Basics',
    items: [
      t('Shell command', 'shell', `    - name: Run a shell command\n      ansible.builtin.shell: echo "hello from SLEP"\n      register: cmd_out\n`),
      t('Command (no shell)', 'command', `    - name: Run a command\n      ansible.builtin.command: uptime\n      register: cmd_out\n`),
      t('Debug message', 'debug print', `    - name: Show a message\n      ansible.builtin.debug:\n        msg: "value is {{ cmd_out.stdout | default('n/a') }}"\n`),
      t('Ping', 'ping', `    - name: Ping the host\n      ansible.builtin.ping:\n`),
      t('Gather facts', 'setup facts', `    - name: Gather facts\n      ansible.builtin.setup:\n`),
      t('Assert a condition', 'assert check', `    - name: Assert a condition\n      ansible.builtin.assert:\n        that:\n          - ansible_facts['os_family'] is defined\n        fail_msg: "condition not met"\n`),
      t('Wait for a port / file', 'wait_for port ready block until listen', `    - name: Wait for a port to accept connections\n      ansible.builtin.wait_for:\n        host: 127.0.0.1\n        port: 8080\n        state: started\n        timeout: 60\n`),
    ],
  },
  {
    group: 'Users & Groups',
    items: [
      t('Create user', 'user account add', `    - name: Create a user\n      ansible.builtin.user:\n        name: deploy\n        groups: sudo,wheel\n        append: true\n        shell: /bin/bash\n        create_home: true\n      become: true\n`),
      t('Add a user to group(s)', 'user group append supplementary member usermod add', `    - name: Add an existing user to supplementary groups\n      ansible.builtin.user:\n        name: deploy\n        groups: docker,sudo\n        append: true      # WITHOUT this, 'groups' REPLACES the user's memberships\n      become: true\n`),
      t('Set / lock password', 'password lock passwd', `    - name: Set a user's password (hashed) and unlock\n      ansible.builtin.user:\n        name: deploy\n        password: "{{ 'changeme' | password_hash('sha512') }}"\n        password_lock: false\n      become: true\n`),
      t('Create group', 'group', `    - name: Create a group\n      ansible.builtin.group:\n        name: engineering\n        state: present\n      become: true\n`),
      t('Sudo access (drop-in)', 'sudo sudoers wheel', `    - name: Grant passwordless sudo to a group\n      ansible.builtin.copy:\n        dest: /etc/sudoers.d/engineering\n        content: "%engineering ALL=(ALL) NOPASSWD:ALL\\n"\n        mode: "0440"\n        validate: 'visudo -cf %s'\n      become: true\n`),
      t('Authorized SSH key', 'ssh key authorized', `    - name: Add an authorized SSH key\n      ansible.posix.authorized_key:\n        user: deploy\n        key: "{{ lookup('file', 'files/id_ed25519.pub') }}"\n      become: true\n`),
    ],
  },
  {
    group: 'Packages & Repositories',
    items: [
      t('Install packages (any distro)', 'package install', `    - name: Install packages\n      ansible.builtin.package:\n        name:\n          - your-package\n        state: present\n      become: true\n`),
      t('Install (apt)', 'apt debian ubuntu', `    - name: Install apt packages\n      ansible.builtin.apt:\n        # Don't pin an exact build (e.g. name: nginx=1.24.0-2ubuntu7.11): Ubuntu keeps\n        # only the current build in the pool, so a pinned one 404s once a security\n        # update supersedes it. Name the package alone for the current version.\n        name: [your-package]\n        state: present\n        update_cache: true\n        cache_valid_time: 3600\n      become: true\n`),
      t('Install (dnf)', 'dnf yum rocky fedora rhel', `    - name: Install dnf packages\n      ansible.builtin.dnf:\n        name: [your-package]\n        state: present\n      become: true\n`),
      t('Install Python packages (pip)', 'pip python packages pip3 requirements', `    - name: Install Python packages\n      ansible.builtin.pip:\n        name:\n          - requests\n        state: present\n      become: true\n`),
      t('Update all packages', 'upgrade update patch', `    - name: Upgrade all packages\n      ansible.builtin.package:\n        name: "*"\n        state: latest\n      become: true\n`),
      t('Add apt repository', 'repo apt ppa', `    - name: Add an apt repository\n      ansible.builtin.apt_repository:\n        repo: "deb https://example.com/apt stable main"\n        state: present\n      become: true\n`),
      t('Add yum/dnf repository', 'repo yum dnf', `    - name: Add a yum/dnf repository\n      ansible.builtin.yum_repository:\n        name: example\n        description: Example repo\n        baseurl: https://example.com/rpm/\n        gpgcheck: false\n      become: true\n`),
    ],
  },
  {
    group: 'Services & systemd',
    items: [
      t('Service state', 'service systemd start enable', `    - name: Ensure a service is running and enabled\n      ansible.builtin.service:\n        name: your-service\n        state: started\n        enabled: true\n      become: true\n`),
      t('Restart service', 'restart service', `    - name: Restart a service\n      ansible.builtin.service:\n        name: your-service\n        state: restarted\n      become: true\n`),
      t('Install a systemd unit', 'systemd unit service file', `    - name: Install a systemd unit\n      ansible.builtin.copy:\n        dest: /etc/systemd/system/myapp.service\n        mode: "0644"\n        content: |\n          [Unit]\n          Description=My App\n          [Service]\n          ExecStart=/usr/local/bin/myapp\n          [Install]\n          WantedBy=multi-user.target\n      become: true\n      notify: reload systemd\n`),
      t('Reload systemd daemon', 'daemon-reload systemctl', `    - name: Reload systemd\n      ansible.builtin.systemd:\n        daemon_reload: true\n      become: true\n`),
      t('Systemd timer', 'timer cron systemd schedule', `    - name: Enable a systemd timer\n      ansible.builtin.systemd:\n        name: mytask.timer\n        state: started\n        enabled: true\n      become: true\n`),
    ],
  },
  {
    group: 'Remove & teardown',
    items: [
      t('Remove packages (any distro)', 'remove uninstall package delete absent', `    - name: Remove packages\n      ansible.builtin.package:\n        name:\n          - your-package\n        state: absent\n      become: true\n`),
      t('Remove packages (apt)', 'remove uninstall apt purge debian ubuntu', `    - name: Remove apt packages\n      ansible.builtin.apt:\n        name: [your-package]\n        state: absent\n        purge: true\n        autoremove: true\n      become: true\n`),
      t('Remove packages (dnf)', 'remove uninstall dnf yum rocky fedora rhel', `    - name: Remove dnf packages\n      ansible.builtin.dnf:\n        name: [your-package]\n        state: absent\n        autoremove: true\n      become: true\n`),
      t('Stop & disable a service', 'stop disable service systemd remove', `    - name: Stop and disable a service\n      ansible.builtin.service:\n        name: your-service\n        state: stopped\n        enabled: false\n      become: true\n`),
      t('Mask a service', 'mask service systemd block', `    - name: Mask a service (block it from starting)\n      ansible.builtin.systemd:\n        name: your-service\n        masked: true\n      become: true\n`),
      t('Remove a systemd unit', 'remove delete systemd unit service file', `    - name: Remove a systemd unit\n      ansible.builtin.file:\n        path: /etc/systemd/system/myapp.service\n        state: absent\n      become: true\n      notify: reload systemd\n`),
      t('Delete a file or directory', 'remove delete file directory rm absent', `    - name: Remove a file or directory\n      ansible.builtin.file:\n        path: /etc/app/old.conf\n        state: absent\n      become: true\n`),
      t('Remove a user', 'remove delete user account userdel', `    - name: Remove a user and their home\n      ansible.builtin.user:\n        name: olduser\n        state: absent\n        remove: true\n      become: true\n`),
      t('Remove a group', 'remove delete group', `    - name: Remove a group\n      ansible.builtin.group:\n        name: oldgroup\n        state: absent\n      become: true\n`),
      t('Remove a cron job', 'remove delete cron crontab', `    - name: Remove a cron job\n      ansible.builtin.cron:\n        name: "nightly backup"\n        state: absent\n      become: true\n`),
      t('Remove an authorized SSH key', 'remove delete ssh key authorized', `    - name: Remove an authorized SSH key\n      ansible.posix.authorized_key:\n        user: deploy\n        key: "{{ lookup('file', 'files/id_ed25519.pub') }}"\n        state: absent\n      become: true\n`),
      t('Remove an apt repository', 'remove delete apt repository ppa', `    - name: Remove an apt repository\n      ansible.builtin.apt_repository:\n        repo: "deb https://example.com/apt stable main"\n        state: absent\n      become: true\n`),
      t('Unmount & remove from fstab', 'unmount remove umount fstab absent', `    - name: Unmount and drop the fstab entry\n      ansible.posix.mount:\n        path: /data\n        state: absent\n      become: true\n`),
    ],
  },
  {
    group: 'Cron & Environment',
    items: [
      t('Cron job', 'cron schedule crontab', `    - name: Schedule a cron job\n      ansible.builtin.cron:\n        name: "nightly backup"\n        minute: "0"\n        hour: "2"\n        job: "/usr/local/bin/backup.sh"\n      become: true\n`),
      t('Environment variable (system-wide)', 'env variable profile.d shell', `    - name: Set a system-wide environment variable\n      ansible.builtin.copy:\n        dest: /etc/profile.d/myenv.sh\n        content: "export MYVAR=value\\n"\n        mode: "0644"\n      become: true\n`),
      t('Shell alias (system-wide)', 'alias shell profile', `    - name: Add a system-wide shell alias\n      ansible.builtin.copy:\n        dest: /etc/profile.d/aliases.sh\n        content: "alias ll='ls -alF'\\n"\n        mode: "0644"\n      become: true\n`),
    ],
  },
  {
    group: 'Files & Permissions',
    items: [
      t('Copy file', 'copy file', `    - name: Copy a file\n      ansible.builtin.copy:\n        src: files/app.conf\n        dest: /etc/app/app.conf\n        owner: root\n        group: root\n        mode: "0644"\n      become: true\n`),
      t('Download a file', 'download get_url url http fetch curl wget', `    - name: Download a file\n      ansible.builtin.get_url:\n        url: https://example.com/app.tar.gz\n        dest: /opt/app.tar.gz\n        mode: "0644"\n      become: true\n`),
      t('Template (Jinja2)', 'template jinja', `    - name: Render a template\n      ansible.builtin.template:\n        src: templates/app.conf.j2\n        dest: /etc/app/app.conf\n        mode: "0644"\n      become: true\n`),
      t('Directory / permissions', 'file directory mkdir chmod chown', `    - name: Ensure a directory exists with permissions\n      ansible.builtin.file:\n        path: /opt/app\n        state: directory\n        owner: deploy\n        group: deploy\n        mode: "0755"\n      become: true\n`),
      t('Line in file', 'lineinfile config edit', `    - name: Ensure a config line is present\n      ansible.builtin.lineinfile:\n        path: /etc/sysctl.conf\n        regexp: '^net.ipv4.ip_forward'\n        line: 'net.ipv4.ip_forward = 1'\n      become: true\n`),
      t('Set ACL', 'acl permissions setfacl', `    - name: Set a file ACL\n      ansible.posix.acl:\n        path: /srv/share\n        entity: deploy\n        etype: user\n        permissions: rwx\n        state: present\n      become: true\n`),
    ],
  },
  {
    group: 'Storage & LVM',
    items: [
      t('Volume group (LVM)', 'lvm lvg volume group pv', `    - name: Create an LVM volume group\n      community.general.lvg:\n        vg: data_vg\n        pvs: /dev/sdb\n      become: true\n`),
      t('Logical volume (LVM)', 'lvm lvol logical volume', `    - name: Create a logical volume\n      community.general.lvol:\n        vg: data_vg\n        lv: data_lv\n        size: 20g\n      become: true\n`),
      t('Create filesystem', 'filesystem mkfs xfs ext4', `    - name: Create a filesystem\n      community.general.filesystem:\n        fstype: xfs\n        dev: /dev/data_vg/data_lv\n      become: true\n`),
      t('Partition (parted)', 'partition parted disk', `    - name: Create a partition\n      community.general.parted:\n        device: /dev/sdb\n        number: 1\n        state: present\n      become: true\n`),
      t('Mount & persist', 'mount fstab', `    - name: Mount and persist a filesystem\n      ansible.posix.mount:\n        path: /data\n        src: /dev/data_vg/data_lv\n        fstype: xfs\n        state: mounted\n      become: true\n`),
      t('LVM snapshot', 'snapshot lvm backup', `    - name: Create an LVM snapshot\n      community.general.lvol:\n        vg: data_vg\n        lv: data_lv\n        snapshot: data_snap\n        size: 5g\n      become: true\n`),
    ],
  },
  {
    group: 'Networking',
    items: [
      t('Configure interface (nmcli)', 'network ip nmcli static dhcp', `    - name: Configure a static IP with nmcli\n      community.general.nmcli:\n        conn_name: eth0\n        ifname: eth0\n        type: ethernet\n        ip4: 192.168.1.50/24\n        gw4: 192.168.1.1\n        dns4: [1.1.1.1]\n        state: present\n      become: true\n`),
      t('Set hostname', 'hostname', `    - name: Set the hostname\n      ansible.builtin.hostname:\n        name: web01\n      become: true\n`),
      t('Manage /etc/hosts entry', 'hosts dns hostfile', `    - name: Add an /etc/hosts entry\n      ansible.builtin.lineinfile:\n        path: /etc/hosts\n        line: "192.168.1.60 db01"\n      become: true\n`),
      t('Kernel parameter (sysctl)', 'sysctl kernel network tuning', `    - name: Set a kernel parameter\n      ansible.posix.sysctl:\n        name: net.ipv4.ip_forward\n        value: "1"\n        state: present\n        reload: true\n      become: true\n`),
    ],
  },
  {
    group: 'Firewall',
    items: [
      t('Open a port (firewalld)', 'firewall firewalld port', `    - name: Open a firewalld port\n      ansible.posix.firewalld:\n        port: 8080/tcp\n        permanent: true\n        immediate: true\n        state: enabled\n      become: true\n`),
      t('Allow a service (firewalld)', 'firewall firewalld service', `    - name: Allow a firewalld service\n      ansible.posix.firewalld:\n        service: https\n        permanent: true\n        immediate: true\n        state: enabled\n      become: true\n`),
      t('Rule (ufw)', 'firewall ufw ubuntu', `    - name: Allow a port with ufw\n      community.general.ufw:\n        rule: allow\n        port: "22"\n        proto: tcp\n      become: true\n`),
    ],
  },
  {
    group: 'Security',
    items: [
      t('SELinux mode', 'selinux enforcing security', `    - name: Set SELinux to enforcing\n      ansible.posix.selinux:\n        policy: targeted\n        state: enforcing\n      become: true\n`),
      t('Harden SSH', 'ssh hardening sshd security', `    - name: Disable SSH root login\n      ansible.builtin.lineinfile:\n        path: /etc/ssh/sshd_config\n        regexp: '^#?PermitRootLogin'\n        line: 'PermitRootLogin no'\n      become: true\n      notify: restart sshd\n`),
      t('Password policy (pwquality)', 'password policy pam pwquality', `    - name: Set minimum password length\n      ansible.builtin.lineinfile:\n        path: /etc/security/pwquality.conf\n        regexp: '^#?minlen'\n        line: 'minlen = 14'\n      become: true\n`),
      t('Install security updates', 'security updates patch', `    - name: Apply security updates (apt)\n      ansible.builtin.apt:\n        upgrade: dist\n        update_cache: true\n      become: true\n      when: ansible_facts['os_family'] == 'Debian'\n`),
    ],
  },
  {
    group: 'Time & Certificates',
    items: [
      t('Set timezone', 'timezone time clock', `    - name: Set the system timezone\n      community.general.timezone:\n        name: America/New_York\n      become: true\n`),
      t('NTP / chrony', 'ntp chrony time sync', `    - name: Install and enable chrony\n      block:\n        - ansible.builtin.package: { name: chrony, state: present }\n        - ansible.builtin.service: { name: chronyd, state: started, enabled: true }\n      become: true\n`),
      t('Private key + self-signed cert', 'certificate tls ssl openssl', `    - name: Generate a private key\n      community.crypto.openssl_privatekey:\n        path: /etc/ssl/private/app.key\n      become: true\n    - name: Generate a self-signed certificate\n      community.crypto.x509_certificate:\n        path: /etc/ssl/certs/app.crt\n        privatekey_path: /etc/ssl/private/app.key\n        provider: selfsigned\n      become: true\n`),
    ],
  },
  {
    group: 'Containers & Directory Services',
    items: [
      t('Docker container', 'docker container run', `    - name: Run a Docker container\n      community.docker.docker_container:\n        name: web\n        image: nginx:latest\n        state: started\n        ports: ["8080:80"]\n`),
      t('Podman container', 'podman container run', `    - name: Run a Podman container\n      containers.podman.podman_container:\n        name: web\n        image: docker.io/library/nginx:latest\n        state: started\n        ports: ["8080:80"]\n`),
      t('Join Active Directory (realm)', 'active directory realm ad sssd join', `    - name: Join the host to Active Directory\n      ansible.builtin.command: "realm join --user={{ ad_user }} example.com"\n      args:\n        creates: /etc/sssd/sssd.conf\n      become: true\n`),
    ],
  },
  {
    group: 'Backup & Maintenance',
    items: [
      t('Archive files', 'backup archive tar compress', `    - name: Archive a directory\n      community.general.archive:\n        path: /etc/app\n        dest: /backups/app.tgz\n        format: gz\n      become: true\n`),
      t('Extract an archive', 'unarchive extract tar untar unzip decompress', `    - name: Extract an archive already on the host\n      ansible.builtin.unarchive:\n        src: /opt/app.tar.gz\n        dest: /opt/app\n        remote_src: true   # false = copy the archive from the control node first\n      become: true\n`),
      t('Rsync (synchronize)', 'backup rsync sync copy', `    - name: Sync a directory\n      ansible.posix.synchronize:\n        src: /data/\n        dest: /backups/data/\n`),
      t('Reboot the host', 'reboot restart', `    - name: Reboot the host\n      ansible.builtin.reboot:\n        reboot_timeout: 600\n      become: true\n`),
      t('OS release upgrade (assess)', 'upgrade release leapp do-release-upgrade', `    - name: Check for a distribution upgrade (Ubuntu)\n      ansible.builtin.command: do-release-upgrade -c\n      register: relup\n      changed_when: false\n      failed_when: false\n      become: true\n`),
      t('Git checkout', 'git clone repo source', `    - name: Check out a repository\n      ansible.builtin.git:\n        repo: https://example.com/app.git\n        dest: /opt/app\n        version: main\n      become: true\n`),
    ],
  },
]
