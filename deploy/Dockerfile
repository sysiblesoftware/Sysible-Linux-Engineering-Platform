# Sysible Linux Engineering Platform — Community Edition, single container.
#
# One image runs both services (backend :9100 + web console :8810) with the
# engines baked in (Ansible + Terraform; Salt is optional, see below). SQLite +
# project workdirs + run logs live on the /data volume. This is the "less of a
# hassle than AAP" promise: docker compose up, open :8810, done.

# ---- stage 1: build the React console -------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY webgui/frontend/package.json webgui/frontend/package-lock.json ./
RUN npm ci
COPY webgui/frontend/ ./
RUN npm run build      # -> /web/dist

# ---- stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim

# Engines + tooling: ssh client (Ansible/salt-ssh transport), git (SCM projects),
# curl/unzip (fetch terraform), ca-certificates, bash (entrypoint).
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash git openssh-client curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Terraform (arch-aware via buildx's TARGETARCH: amd64 / arm64).
ARG TARGETARCH=amd64
ARG TERRAFORM_VERSION=1.9.8
RUN curl -fsSL -o /tmp/tf.zip \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
    && unzip -o /tmp/tf.zip -d /usr/local/bin \
    && rm /tmp/tf.zip \
    && terraform version

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
# Ansible is first-class, so bake ansible-core. (Salt is optional — uncomment to
# bake salt-ssh; it's a large dependency, hence off by default for a lean image.)
RUN pip install --no-cache-dir -r backend/requirements.txt ansible-core
#   && pip install --no-cache-dir salt

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
ENTRYPOINT ["/entrypoint.sh"]
