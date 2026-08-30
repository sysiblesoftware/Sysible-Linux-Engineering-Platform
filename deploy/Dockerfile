# Sysible Linux Engineering Platform — Community Edition, single container.
#
# One image runs both services (backend :9100 + web console :8810) with all three
# engines baked in (Ansible + Terraform + Salt; Salt is toggleable via the
# INSTALL_SALT build arg). SQLite + project workdirs + run logs live on the /data
# volume. This is the "less of a hassle than AAP" promise: docker compose up,
# open :8810, done.

# ---- stage 1: build the React console -------------------------------------
FROM node:22-slim AS web
WORKDIR /web
# URL prefix the console is served under: "/" standalone, "/slep/" behind the SLOP
# gateway (which path-routes /slep/* to this app on one shared origin). Vite bakes
# it into the asset URLs + the SPA's API base (see webgui/frontend/vite.config.js).
# The SLOP installer passes /slep/ via --build-arg; a plain build keeps root.
ARG SYSIBLE_BASE_PATH=/
ENV SYSIBLE_BASE_PATH=${SYSIBLE_BASE_PATH}
COPY webgui/frontend/package.json webgui/frontend/package-lock.json ./
RUN npm ci
COPY webgui/frontend/ ./
RUN npm run build      # -> /web/dist

# ---- stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim

# Engines + tooling: ssh client (Ansible/salt-ssh transport), sshpass (password
# SSH auth for Ansible), git (SCM projects), curl/unzip (fetch terraform),
# libvirt-clients (`virsh` — the "Test hypervisor connection" probe for the
# libvirt/KVM VM builder), genisoimage (the dmacvicar/libvirt provider builds
# each VM's cloud-init ISO by shelling out to `mkisofs` — provide it, symlinked
# from genisoimage), ca-certificates, bash (entrypoint).
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash git openssh-client sshpass curl unzip ca-certificates libvirt-clients genisoimage \
    && ln -sf "$(command -v genisoimage)" /usr/local/bin/mkisofs \
    && rm -rf /var/lib/apt/lists/*

# Terraform (arch-aware via buildx's TARGETARCH: amd64 / arm64).
ARG TARGETARCH=amd64
ARG TERRAFORM_VERSION=1.9.8
# Download the pinned Terraform and verify its SHA-256 against HashiCorp's
# published SHA256SUMS before unzipping (so a corrupted/substituted artifact from
# a mirror/MITM is rejected, not installed).
RUN TFZIP="terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
    && curl -fsSL -o "/tmp/${TFZIP}" \
         "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/${TFZIP}" \
    && curl -fsSL -o /tmp/tf.sums \
         "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
    && (cd /tmp && grep " ${TFZIP}\$" tf.sums | sha256sum -c -) \
    && unzip -o "/tmp/${TFZIP}" -d /usr/local/bin \
    && rm -f "/tmp/${TFZIP}" /tmp/tf.sums \
    && terraform version

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
# Ansible is first-class. Install the full `ansible` package (not just
# ansible-core) so the community.general / ansible.posix collections the task
# palette uses (LVM, mount, firewalld, selinux, authorized_key, …) are present.
# sshpass (installed above) covers password SSH.
RUN pip install --no-cache-dir -r backend/requirements.txt ansible

# Salt (salt-ssh) is the third engine. It pulls C-extension deps (timelib) that
# must be compiled, so we add a toolchain, build the wheels, then purge it in the
# SAME layer — salt stays, the ~200 MB of build tools don't. Set INSTALL_SALT=0
# (--build-arg) for a leaner image without Salt; the in-app installer can add it
# later on a host that has a compiler.
ARG INSTALL_SALT=1
RUN if [ "$INSTALL_SALT" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends gcc python3-dev libffi-dev \
      && pip install --no-cache-dir --prefer-binary salt \
      && apt-get purge -y gcc python3-dev libffi-dev && apt-get autoremove -y \
      && rm -rf /var/lib/apt/lists/* ; \
    fi

COPY backend/ backend/
COPY webgui/ webgui/
COPY deploy/entrypoint.sh /entrypoint.sh
COPY --from=web /web/dist webgui/frontend/dist

ENV PYTHONPATH=/app \
    SLEP_DATA_DIR=/data \
    SLEP_BACKEND_URL=http://127.0.0.1:9100 \
    SLEP_CONTAINER=1 \
    SLEP_TLS=1 \
    PYTHONUNBUFFERED=1

# Non-root. Declare the volume AFTER chown so the named volume inherits slep's
# ownership (Docker seeds a fresh named volume from the image path).
RUN chmod +x /entrypoint.sh \
    && useradd -r -m -d /home/slep slep \
    && mkdir -p /data \
    && chown -R slep:slep /app /data
USER slep
VOLUME ["/data"]
EXPOSE 8810

# Liveness: the console answers on 8810. Works in both modes — HTTPS (self-signed,
# hence -k) by default, or plain HTTP when SLEP_TLS=0.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsk https://127.0.0.1:8810/api/health >/dev/null 2>&1 \
   || curl -fs  http://127.0.0.1:8810/api/health  >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
