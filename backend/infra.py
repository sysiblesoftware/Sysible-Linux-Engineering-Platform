"""Create Infrastructure — a form-driven Terraform VM builder.

The console shows menus (from PROVIDERS below); this module turns the selections
into a real Terraform project (main.tf / variables.tf / outputs.tf + a cloud-init
file) written into a SLEP project. Every provider emits a normalized
`output "sysible_hosts"` — a list of {name, ip, user} — so after `terraform apply`
the auto-enroll step can read the created VMs back and register them in a
connected Sysible Controller (see app.py /infra/{id}/enroll).

The generated HCL is deliberately plain and variable-driven so it reads well in
the IDE and is easy to tweak by hand afterwards.
"""
from __future__ import annotations

# ---- option schema shown as menus in the console ---------------------------
# type: "select" (choices), "number", "text", "textarea". `default` seeds the form.
_COUNT = {"key": "count", "label": "How many VMs", "type": "number", "default": 2}
_PREFIX = {"key": "name_prefix", "label": "Name prefix", "type": "text", "default": "app"}
_SSH_USER = {"key": "ssh_user", "label": "Login user", "type": "text", "default": "ubuntu"}
_SSH_KEY = {"key": "ssh_public_key", "label": "Deploy SSH public key (for SLEP access)",
            "type": "textarea", "default": "", "help": "Paste an ssh-ed25519/ssh-rsa public key. Optional."}
_ENV = {"key": "environment", "label": "Environment tag (Controller group)", "type": "text", "default": "production"}

PROVIDERS = {
    "aws": {
        "label": "AWS EC2",
        "blurb": "Elastic Compute Cloud instances. Needs AWS credentials (attach a cloud credential).",
        "options": [
            _COUNT, _PREFIX,
            {"key": "region", "label": "Region", "type": "select", "default": "us-east-1",
             "choices": ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-central-1", "ap-southeast-1", "ap-south-1"]},
            {"key": "instance_type", "label": "Instance type", "type": "select", "default": "t3.micro",
             "choices": ["t3.micro", "t3.small", "t3.medium", "t3.large", "t3.xlarge", "m5.large", "m5.xlarge", "c5.large"]},
            {"key": "ami", "label": "AMI id", "type": "text", "default": "ami-0c7217cdde317cfec",
             "help": "Default is Ubuntu 22.04 (us-east-1). Change per region."},
            {"key": "disk_size", "label": "Root disk (GB)", "type": "number", "default": 20},
            _SSH_USER, _SSH_KEY, _ENV,
        ],
    },
    "digitalocean": {
        "label": "DigitalOcean",
        "blurb": "Droplets. Needs a DigitalOcean API token (attach a cloud credential: DIGITALOCEAN_TOKEN=…).",
        "options": [
            _COUNT, _PREFIX,
            {"key": "region", "label": "Region", "type": "select", "default": "nyc3",
             "choices": ["nyc1", "nyc3", "sfo3", "ams3", "fra1", "lon1", "sgp1", "tor1", "blr1"]},
            {"key": "size", "label": "Droplet size", "type": "select", "default": "s-1vcpu-1gb",
             "choices": ["s-1vcpu-1gb", "s-1vcpu-2gb", "s-2vcpu-2gb", "s-2vcpu-4gb", "s-4vcpu-8gb"]},
            {"key": "image", "label": "Image", "type": "select", "default": "ubuntu-22-04-x64",
             "choices": ["ubuntu-22-04-x64", "ubuntu-24-04-x64", "debian-12-x64", "rockylinux-9-x64", "fedora-40-x64"]},
            {"key": "ssh_user", "label": "Login user", "type": "text", "default": "root"},
            _SSH_KEY, _ENV,
        ],
    },
    "libvirt": {
        "label": "libvirt (KVM/QEMU)",
        "blurb": "Local hypervisor VMs via the dmacvicar/libvirt provider. Set the base image + pool below.",
        "options": [
            _COUNT, _PREFIX,
            {"key": "memory", "label": "Memory (MB)", "type": "select", "default": "2048",
             "choices": ["1024", "2048", "4096", "8192", "16384"]},
            {"key": "vcpu", "label": "vCPUs", "type": "select", "default": "2", "choices": ["1", "2", "4", "8"]},
            {"key": "pool", "label": "Storage pool", "type": "text", "default": "default"},
            {"key": "base_image", "label": "Base image URL/path", "type": "text",
             "default": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"},
            {"key": "network", "label": "Network name", "type": "text", "default": "default"},
            _SSH_USER, _SSH_KEY, _ENV,
        ],
    },
    "proxmox": {
        "label": "Proxmox VE",
        "blurb": "Proxmox VMs via the bpg/proxmox provider, cloned from a cloud-init template.",
        "options": [
            _COUNT, _PREFIX,
            {"key": "node", "label": "Proxmox node", "type": "text", "default": "pve"},
            {"key": "template_id", "label": "Clone template VM id", "type": "number", "default": 9000,
             "help": "The VM id of a cloud-init-ready template to clone."},
            {"key": "cores", "label": "Cores", "type": "select", "default": "2", "choices": ["1", "2", "4", "8"]},
            {"key": "memory", "label": "Memory (MB)", "type": "select", "default": "2048",
             "choices": ["1024", "2048", "4096", "8192"]},
            {"key": "disk_size", "label": "Disk (GB)", "type": "number", "default": 20},
            {"key": "datastore", "label": "Datastore", "type": "text", "default": "local-lvm"},
            {"key": "bridge", "label": "Network bridge", "type": "text", "default": "vmbr0"},
            _SSH_USER, _SSH_KEY, _ENV,
        ],
    },
    "gcp": {
        "label": "Google Cloud (GCE)",
        "blurb": "Compute Engine instances. Needs a GCP service-account key (attach a cloud credential: GOOGLE_CREDENTIALS / GOOGLE_APPLICATION_CREDENTIALS) and your project id.",
        "options": [
            _COUNT, _PREFIX,
            {"key": "project", "label": "GCP project id", "type": "text", "default": "my-project"},
            {"key": "region", "label": "Region", "type": "select", "default": "us-central1",
             "choices": ["us-central1", "us-east1", "us-west1", "europe-west1", "europe-west4", "asia-east1", "asia-southeast1"]},
            {"key": "zone", "label": "Zone", "type": "text", "default": "us-central1-a"},
            {"key": "machine_type", "label": "Machine type", "type": "select", "default": "e2-small",
             "choices": ["e2-micro", "e2-small", "e2-medium", "e2-standard-2", "e2-standard-4", "n2-standard-2"]},
            {"key": "image", "label": "Image", "type": "text", "default": "ubuntu-os-cloud/ubuntu-2204-lts"},
            {"key": "disk_size", "label": "Boot disk (GB)", "type": "number", "default": 20},
            _SSH_USER, _SSH_KEY, _ENV,
        ],
    },
    "azure": {
        "label": "Microsoft Azure",
        "blurb": "Linux VMs (with a resource group + network). Needs Azure credentials (attach a cloud credential: ARM_CLIENT_ID/ARM_CLIENT_SECRET/ARM_TENANT_ID/ARM_SUBSCRIPTION_ID). SSH key must be RSA.",
        "options": [
            _COUNT, _PREFIX,
            {"key": "location", "label": "Region", "type": "select", "default": "eastus",
             "choices": ["eastus", "eastus2", "westus2", "westeurope", "northeurope", "uksouth", "southeastasia", "australiaeast"]},
            {"key": "vm_size", "label": "VM size", "type": "select", "default": "Standard_B1s",
             "choices": ["Standard_B1s", "Standard_B2s", "Standard_D2s_v5", "Standard_D4s_v5", "Standard_F2s_v2"]},
            {"key": "ssh_user", "label": "Admin user", "type": "text", "default": "azureuser"},
            {"key": "ssh_public_key", "label": "Deploy SSH public key (RSA — Azure requires it)",
             "type": "textarea", "default": "", "help": "Azure's admin_ssh_key needs an RSA key (ssh-rsa …)."},
            _ENV,
        ],
    },
}


def provider_schema() -> dict:
    """Menus for the console — providers with their option lists (no render fns)."""
    return {p: {"label": v["label"], "blurb": v["blurb"], "options": v["options"]}
            for p, v in PROVIDERS.items()}


def _opt(spec, key, default=""):
    return spec.get(key, default)


def _cloudinit(ssh_user: str, keys: list[str]) -> str:
    clean = [k.strip() for k in keys if k and k.strip()]
    lines = ["#cloud-config", "users:", f"  - name: {ssh_user}",
             "    sudo: ALL=(ALL) NOPASSWD:ALL", "    shell: /bin/bash",
             "    ssh_authorized_keys:"]
    if clean:
        lines += [f"      - {k}" for k in clean]
    else:
        lines.append("      []")
    lines.append("package_update: true")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- renderers
def _render_aws(spec, keys):
    ssh_user = _opt(spec, "ssh_user", "ubuntu")
    main = f'''terraform {{
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = "~> 5.0" }}
  }}
}}

provider "aws" {{
  region = var.region
}}

resource "aws_instance" "vm" {{
  count         = var.vm_count
  ami           = var.ami
  instance_type = var.instance_type
  user_data     = file("${{path.module}}/cloudinit.cfg")

  root_block_device {{
    volume_size = var.disk_size
  }}
  tags = {{
    Name        = "${{var.name_prefix}}-${{count.index + 1}}"
    Environment = var.environment
    ManagedBy   = "SLEP"
  }}
}}
'''
    variables = _vars({
        "region": ("string", spec.get("region", "us-east-1")),
        "instance_type": ("string", spec.get("instance_type", "t3.micro")),
        "ami": ("string", spec.get("ami", "")),
        "disk_size": ("number", spec.get("disk_size", 20)),
        "vm_count": ("number", spec.get("count", 2)),
        "name_prefix": ("string", spec.get("name_prefix", "app")),
        "environment": ("string", spec.get("environment", "production")),
    })
    outputs = _outputs("aws_instance.vm", "public_ip", ssh_user)
    return {"main.tf": main, "variables.tf": variables, "outputs.tf": outputs,
            "cloudinit.cfg": _cloudinit(ssh_user, keys)}


def _render_digitalocean(spec, keys):
    ssh_user = _opt(spec, "ssh_user", "root")
    main = f'''terraform {{
  required_providers {{
    digitalocean = {{ source = "digitalocean/digitalocean", version = "~> 2.0" }}
  }}
}}

provider "digitalocean" {{}}  # token via DIGITALOCEAN_TOKEN (attach a cloud credential)

resource "digitalocean_droplet" "vm" {{
  count     = var.vm_count
  name      = "${{var.name_prefix}}-${{count.index + 1}}"
  region    = var.region
  size      = var.size
  image     = var.image
  user_data = file("${{path.module}}/cloudinit.cfg")
  tags      = [var.environment, "slep"]
}}
'''
    variables = _vars({
        "region": ("string", spec.get("region", "nyc3")),
        "size": ("string", spec.get("size", "s-1vcpu-1gb")),
        "image": ("string", spec.get("image", "ubuntu-22-04-x64")),
        "vm_count": ("number", spec.get("count", 2)),
        "name_prefix": ("string", spec.get("name_prefix", "app")),
        "environment": ("string", spec.get("environment", "production")),
    })
    outputs = _outputs("digitalocean_droplet.vm", "ipv4_address", ssh_user)
    return {"main.tf": main, "variables.tf": variables, "outputs.tf": outputs,
            "cloudinit.cfg": _cloudinit(ssh_user, keys)}


def _render_libvirt(spec, keys):
    ssh_user = _opt(spec, "ssh_user", "ubuntu")
    main = f'''terraform {{
  required_providers {{
    libvirt = {{ source = "dmacvicar/libvirt", version = "~> 0.7" }}
  }}
}}

provider "libvirt" {{
  uri = "qemu:///system"
}}

resource "libvirt_cloudinit_disk" "ci" {{
  count     = var.vm_count
  name      = "${{var.name_prefix}}-${{count.index + 1}}-ci.iso"
  pool      = var.pool
  user_data = file("${{path.module}}/cloudinit.cfg")
}}

resource "libvirt_volume" "disk" {{
  count            = var.vm_count
  name             = "${{var.name_prefix}}-${{count.index + 1}}.qcow2"
  pool             = var.pool
  source           = var.base_image
  format           = "qcow2"
}}

resource "libvirt_domain" "vm" {{
  count     = var.vm_count
  name      = "${{var.name_prefix}}-${{count.index + 1}}"
  memory    = var.memory
  vcpu      = var.vcpu
  cloudinit = libvirt_cloudinit_disk.ci[count.index].id

  disk {{ volume_id = libvirt_volume.disk[count.index].id }}
  network_interface {{
    network_name   = var.network
    wait_for_lease = true
  }}
  console {{
    type        = "pty"
    target_type = "serial"
    target_port = "0"
  }}
}}
'''
    variables = _vars({
        "memory": ("number", int(spec.get("memory", 2048))),
        "vcpu": ("number", int(spec.get("vcpu", 2))),
        "pool": ("string", spec.get("pool", "default")),
        "base_image": ("string", spec.get("base_image", "")),
        "network": ("string", spec.get("network", "default")),
        "vm_count": ("number", spec.get("count", 2)),
        "name_prefix": ("string", spec.get("name_prefix", "app")),
        "environment": ("string", spec.get("environment", "production")),
    })
    # libvirt exposes the DHCP address at network_interface.0.addresses.0
    outputs = f'''output "sysible_hosts" {{
  value = [ for i, d in libvirt_domain.vm : {{
    name = "${{var.name_prefix}}-${{i + 1}}"
    ip   = try(d.network_interface[0].addresses[0], "")
    user = "{ssh_user}"
  }} ]
}}
'''
    return {"main.tf": main, "variables.tf": variables, "outputs.tf": outputs,
            "cloudinit.cfg": _cloudinit(ssh_user, keys)}


def _render_proxmox(spec, keys):
    ssh_user = _opt(spec, "ssh_user", "ubuntu")
    key_hcl = ", ".join(f'"{k.strip()}"' for k in keys if k and k.strip()) or ""
    main = f'''terraform {{
  required_providers {{
    proxmox = {{ source = "bpg/proxmox", version = "~> 0.60" }}
  }}
}}

provider "proxmox" {{}}  # endpoint/token via PROXMOX_VE_* env (attach a cloud credential)

resource "proxmox_virtual_environment_vm" "vm" {{
  count     = var.vm_count
  name      = "${{var.name_prefix}}-${{count.index + 1}}"
  node_name = var.node

  clone {{
    vm_id     = var.template_id
    node_name = var.node
  }}
  cpu {{
    cores = var.cores
  }}
  memory {{
    dedicated = var.memory
  }}
  disk {{
    datastore_id = var.datastore
    interface    = "scsi0"
    size         = var.disk_size
  }}
  network_device {{
    bridge = var.bridge
  }}
  initialization {{
    ip_config {{
      ipv4 {{
        address = "dhcp"
      }}
    }}
    user_account {{
      username = "{ssh_user}"
      keys     = [{key_hcl}]
    }}
  }}
}}
'''
    variables = _vars({
        "node": ("string", spec.get("node", "pve")),
        "template_id": ("number", int(spec.get("template_id", 9000))),
        "cores": ("number", int(spec.get("cores", 2))),
        "memory": ("number", int(spec.get("memory", 2048))),
        "disk_size": ("number", spec.get("disk_size", 20)),
        "datastore": ("string", spec.get("datastore", "local-lvm")),
        "bridge": ("string", spec.get("bridge", "vmbr0")),
        "vm_count": ("number", spec.get("count", 2)),
        "name_prefix": ("string", spec.get("name_prefix", "app")),
        "environment": ("string", spec.get("environment", "production")),
    })
    outputs = f'''output "sysible_hosts" {{
  value = [ for i, v in proxmox_virtual_environment_vm.vm : {{
    name = "${{var.name_prefix}}-${{i + 1}}"
    ip   = try(v.ipv4_addresses[1][0], "")
    user = "{ssh_user}"
  }} ]
}}
'''
    return {"main.tf": main, "variables.tf": variables, "outputs.tf": outputs}


def _render_gcp(spec, keys):
    ssh_user = _opt(spec, "ssh_user", "ubuntu")
    # GCE injects SSH keys via instance metadata `ssh-keys` (newline-separated
    # "user:key" lines) — no cloud-init file needed.
    ssh_keys = "\\n".join(f"{ssh_user}:{k.strip()}" for k in keys if k and k.strip())
    main = f'''terraform {{
  required_providers {{
    google = {{ source = "hashicorp/google", version = "~> 5.0" }}
  }}
}}

provider "google" {{
  project = var.project
  region  = var.region
}}

resource "google_compute_instance" "vm" {{
  count        = var.vm_count
  name         = "${{var.name_prefix}}-${{count.index + 1}}"
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {{
    initialize_params {{
      image = var.image
      size  = var.disk_size
    }}
  }}
  network_interface {{
    network = "default"
    access_config {{}}   # ephemeral public IP
  }}
  metadata = {{
    ssh-keys = "{ssh_keys}"
  }}
  labels = {{
    environment = var.environment
    managed_by  = "slep"
  }}
}}
'''
    variables = _vars({
        "project": ("string", spec.get("project", "my-project")),
        "region": ("string", spec.get("region", "us-central1")),
        "zone": ("string", spec.get("zone", "us-central1-a")),
        "machine_type": ("string", spec.get("machine_type", "e2-small")),
        "image": ("string", spec.get("image", "ubuntu-os-cloud/ubuntu-2204-lts")),
        "disk_size": ("number", spec.get("disk_size", 20)),
        "vm_count": ("number", spec.get("count", 2)),
        "name_prefix": ("string", spec.get("name_prefix", "app")),
        "environment": ("string", spec.get("environment", "production")),
    })
    outputs = f'''output "sysible_hosts" {{
  value = [ for i, v in google_compute_instance.vm : {{
    name = "${{var.name_prefix}}-${{i + 1}}"
    ip   = try(v.network_interface[0].access_config[0].nat_ip, "")
    user = "{ssh_user}"
  }} ]
}}
'''
    return {"main.tf": main, "variables.tf": variables, "outputs.tf": outputs}


def _render_azure(spec, keys):
    ssh_user = _opt(spec, "ssh_user", "azureuser")
    # admin_ssh_key takes the deploy key; the Controller key (if any) goes in via
    # cloud-init custom_data so it also lands in authorized_keys.
    ctrl_keys = [k for k in keys[1:] if k and k.strip()]
    main = f'''terraform {{
  required_providers {{
    azurerm = {{ source = "hashicorp/azurerm", version = "~> 3.0" }}
  }}
}}

provider "azurerm" {{
  features {{}}
}}

resource "azurerm_resource_group" "rg" {{
  name     = "${{var.name_prefix}}-rg"
  location = var.location
}}

resource "azurerm_virtual_network" "vnet" {{
  name                = "${{var.name_prefix}}-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}}

resource "azurerm_subnet" "subnet" {{
  name                 = "${{var.name_prefix}}-subnet"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.0.1.0/24"]
}}

resource "azurerm_public_ip" "pip" {{
  count               = var.vm_count
  name                = "${{var.name_prefix}}-${{count.index + 1}}-pip"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  allocation_method   = "Static"
}}

resource "azurerm_network_interface" "nic" {{
  count               = var.vm_count
  name                = "${{var.name_prefix}}-${{count.index + 1}}-nic"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  ip_configuration {{
    name                          = "internal"
    subnet_id                     = azurerm_subnet.subnet.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.pip[count.index].id
  }}
}}

resource "azurerm_linux_virtual_machine" "vm" {{
  count                 = var.vm_count
  name                  = "${{var.name_prefix}}-${{count.index + 1}}"
  resource_group_name   = azurerm_resource_group.rg.name
  location              = azurerm_resource_group.rg.location
  size                  = var.vm_size
  admin_username        = var.admin_user
  network_interface_ids = [azurerm_network_interface.nic[count.index].id]
  custom_data           = base64encode(file("${{path.module}}/cloudinit.cfg"))

  admin_ssh_key {{
    username   = var.admin_user
    public_key = var.ssh_public_key
  }}
  os_disk {{
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }}
  source_image_reference {{
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }}
}}
'''
    variables = _vars({
        "location": ("string", spec.get("location", "eastus")),
        "vm_size": ("string", spec.get("vm_size", "Standard_B1s")),
        "admin_user": ("string", ssh_user),
        "ssh_public_key": ("string", spec.get("ssh_public_key", "")),
        "vm_count": ("number", spec.get("count", 2)),
        "name_prefix": ("string", spec.get("name_prefix", "app")),
        "environment": ("string", spec.get("environment", "production")),
    })
    outputs = f'''output "sysible_hosts" {{
  value = [ for i, v in azurerm_linux_virtual_machine.vm : {{
    name = "${{var.name_prefix}}-${{i + 1}}"
    ip   = try(v.public_ip_address, azurerm_public_ip.pip[i].ip_address, "")
    user = "{ssh_user}"
  }} ]
}}
'''
    return {"main.tf": main, "variables.tf": variables, "outputs.tf": outputs,
            "cloudinit.cfg": _cloudinit(ssh_user, ctrl_keys)}


_RENDERERS = {"aws": _render_aws, "digitalocean": _render_digitalocean,
              "libvirt": _render_libvirt, "proxmox": _render_proxmox,
              "gcp": _render_gcp, "azure": _render_azure}


def _vars(defs: dict) -> str:
    out = []
    for name, (typ, default) in defs.items():
        if typ == "string":
            dv = f'"{default}"'
        else:
            dv = str(default)
        out.append(f'variable "{name}" {{\n  type    = {typ}\n  default = {dv}\n}}\n')
    return "\n".join(out)


def _outputs(resource: str, ip_attr: str, ssh_user: str) -> str:
    return (f'output "sysible_hosts" {{\n'
            f'  value = [ for i, r in {resource} : {{\n'
            f'    name = "${{var.name_prefix}}-${{i + 1}}"\n'
            f'    ip   = r.{ip_attr}\n'
            f'    user = "{ssh_user}"\n'
            f'  }} ]\n}}\n')


def generate(provider: str, spec: dict, controller_key: str = "") -> dict:
    """Return {filename: content} for the chosen provider + options. `controller_key`
    (if given) is injected into cloud-init so a Controller can SSH the VM after boot."""
    if provider not in _RENDERERS:
        raise ValueError(f"unknown provider '{provider}'")
    keys = [spec.get("ssh_public_key", ""), controller_key]
    return _RENDERERS[provider](spec, keys)
