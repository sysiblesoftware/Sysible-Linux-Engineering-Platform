// Ready-made Ansible task snippets for the IDE "Insert task" palette. Each item's
// `yaml` is a complete task block (fully-qualified module names) inserted at the
// cursor. Indented for a play's `tasks:` list (task at 4 spaces). Grouped for the
// palette; `search` lets the palette match on module name too.
export const SNIPPET_GROUPS = [
  {
    group: 'Basics',
    items: [
      { name: 'Shell command', search: 'shell', yaml: `    - name: Run a shell command\n      ansible.builtin.shell: echo "hello from SLEP"\n      register: cmd_out\n` },
      { name: 'Command (no shell)', search: 'command', yaml: `    - name: Run a command\n      ansible.builtin.command: uptime\n      register: cmd_out\n` },
      { name: 'Debug message', search: 'debug', yaml: `    - name: Show a message\n      ansible.builtin.debug:\n        msg: "value is {{ cmd_out.stdout | default('n/a') }}"\n` },
      { name: 'Ping', search: 'ping', yaml: `    - name: Ping the host\n      ansible.builtin.ping:\n` },
      { name: 'Gather facts', search: 'setup facts', yaml: `    - name: Gather facts\n      ansible.builtin.setup:\n` },
    ],
  },
  {
    group: 'Packages & services',
    items: [
      { name: 'Install packages (any distro)', search: 'package install', yaml: `    - name: Install packages\n      ansible.builtin.package:\n        name:\n          - nginx\n        state: present\n      become: true\n` },
      { name: 'Install (apt)', search: 'apt debian ubuntu', yaml: `    - name: Install apt packages\n      ansible.builtin.apt:\n        name: [nginx]\n        state: present\n        update_cache: true\n      become: true\n` },
      { name: 'Install (dnf)', search: 'dnf yum rocky fedora', yaml: `    - name: Install dnf packages\n      ansible.builtin.dnf:\n        name: [nginx]\n        state: present\n      become: true\n` },
      { name: 'Service state', search: 'service systemd start enable', yaml: `    - name: Ensure service is running and enabled\n      ansible.builtin.service:\n        name: nginx\n        state: started\n        enabled: true\n      become: true\n` },
    ],
  },
  {
    group: 'Files',
    items: [
      { name: 'Copy file', search: 'copy', yaml: `    - name: Copy a file\n      ansible.builtin.copy:\n        src: files/app.conf\n        dest: /etc/app/app.conf\n        owner: root\n        group: root\n        mode: "0644"\n      become: true\n` },
      { name: 'Template (Jinja2)', search: 'template jinja', yaml: `    - name: Render a template\n      ansible.builtin.template:\n        src: templates/app.conf.j2\n        dest: /etc/app/app.conf\n        mode: "0644"\n      become: true\n` },
      { name: 'Manage a file / directory', search: 'file directory mkdir', yaml: `    - name: Ensure a directory exists\n      ansible.builtin.file:\n        path: /opt/app\n        state: directory\n        mode: "0755"\n      become: true\n` },
      { name: 'Line in file', search: 'lineinfile config', yaml: `    - name: Ensure a config line is present\n      ansible.builtin.lineinfile:\n        path: /etc/ssh/sshd_config\n        regexp: '^#?PermitRootLogin'\n        line: 'PermitRootLogin no'\n      become: true\n` },
    ],
  },
  {
    group: 'Users & access',
    items: [
      { name: 'Create user', search: 'user account', yaml: `    - name: Create a user\n      ansible.builtin.user:\n        name: deploy\n        groups: sudo\n        shell: /bin/bash\n        create_home: true\n      become: true\n` },
      { name: 'Create group', search: 'group', yaml: `    - name: Create a group\n      ansible.builtin.group:\n        name: engineering\n        state: present\n      become: true\n` },
      { name: 'Authorized SSH key', search: 'ssh key authorized', yaml: `    - name: Add an authorized SSH key\n      ansible.posix.authorized_key:\n        user: deploy\n        key: "{{ lookup('file', 'files/id_ed25519.pub') }}"\n      become: true\n` },
    ],
  },
  {
    group: 'Storage & LVM',
    items: [
      { name: 'Volume group (LVM)', search: 'lvm lvg volume group pv', yaml: `    - name: Create an LVM volume group\n      community.general.lvg:\n        vg: data_vg\n        pvs: /dev/sdb\n      become: true\n` },
      { name: 'Logical volume (LVM)', search: 'lvm lvol logical volume', yaml: `    - name: Create a logical volume\n      community.general.lvol:\n        vg: data_vg\n        lv: data_lv\n        size: 20g\n      become: true\n` },
      { name: 'Create filesystem', search: 'filesystem mkfs xfs ext4', yaml: `    - name: Create a filesystem\n      community.general.filesystem:\n        fstype: xfs\n        dev: /dev/data_vg/data_lv\n      become: true\n` },
      { name: 'Mount', search: 'mount fstab', yaml: `    - name: Mount and persist a filesystem\n      ansible.posix.mount:\n        path: /data\n        src: /dev/data_vg/data_lv\n        fstype: xfs\n        state: mounted\n      become: true\n` },
      { name: 'Partition (parted)', search: 'partition parted disk', yaml: `    - name: Create a partition\n      community.general.parted:\n        device: /dev/sdb\n        number: 1\n        state: present\n      become: true\n` },
    ],
  },
  {
    group: 'Source & scheduling',
    items: [
      { name: 'Git checkout', search: 'git clone repo', yaml: `    - name: Check out a repository\n      ansible.builtin.git:\n        repo: https://example.com/app.git\n        dest: /opt/app\n        version: main\n      become: true\n` },
      { name: 'Download a URL', search: 'get_url download', yaml: `    - name: Download a file\n      ansible.builtin.get_url:\n        url: https://example.com/app.tar.gz\n        dest: /tmp/app.tar.gz\n        mode: "0644"\n` },
      { name: 'Cron job', search: 'cron schedule', yaml: `    - name: Schedule a cron job\n      ansible.builtin.cron:\n        name: "nightly backup"\n        minute: "0"\n        hour: "2"\n        job: "/usr/local/bin/backup.sh"\n      become: true\n` },
      { name: 'Reboot', search: 'reboot restart', yaml: `    - name: Reboot the host\n      ansible.builtin.reboot:\n        reboot_timeout: 600\n      become: true\n` },
    ],
  },
]
