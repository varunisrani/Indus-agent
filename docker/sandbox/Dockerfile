# Indus Agent sandbox images (P23-09, D-16.64): where the Docker terminal backend (and the cloud
# sandboxes that take an image) run the agent's commands.
#
#   docker build -f docker/sandbox/Dockerfile --target slim    -t indus-sandbox:slim .
#   docker build -f docker/sandbox/Dockerfile --target browser -t indus-sandbox:browser .
#
# The backend starts the container itself (`docker run -d --init --cap-drop ALL ... IMAGE sleep
# infinity`) and runs `bash -c` scripts in it, so there is no ENTRYPOINT. The default user stays
# root: the backend mounts skills and credentials read-only under /root/.indus and keeps only the
# capabilities root needs on files it does not own. A non-root user `indus` (10000) is there for
# host-user starts and derived images; its passwordless sudo is off unless SANDBOX_SUDO=1.

ARG BASE_IMAGE=debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad

# ---- slim: a shell toolbox -------------------------------------------------------------------
FROM ${BASE_IMAGE} AS slim
ARG SANDBOX_SUDO=0
LABEL org.opencontainers.image.title="Indus Agent sandbox (slim)" \
      org.opencontainers.image.description="Command sandbox for the Indus Agent Docker backend" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="Indus Agent" \
      org.opencontainers.image.source="https://github.com/indusagi/indus-agent"
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash coreutils findutils grep sed gawk diffutils patch less file \
        ca-certificates curl git openssh-client \
        python3 python3-venv python3-pip \
        build-essential pkg-config \
        ripgrep jq procps xz-utils unzip zip tar gzip bzip2; \
    if [ "${SANDBOX_SUDO}" = 1 ]; then \
        apt-get install -y --no-install-recommends sudo; \
    elif [ "${SANDBOX_SUDO}" != 0 ]; then \
        echo "SANDBOX_SUDO must be 0 or 1" >&2; exit 1; \
    fi; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid 10000 indus; \
    useradd --uid 10000 --gid indus --create-home --home-dir /home/indus --shell /bin/bash indus; \
    if [ "${SANDBOX_SUDO}" = 1 ]; then \
        printf 'indus ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/indus; \
        chmod 0440 /etc/sudoers.d/indus; \
    fi
WORKDIR /root
CMD ["bash"]

# ---- browser: Chromium and a virtual display for the browser and computer-use tools --------
FROM slim AS browser
LABEL org.opencontainers.image.title="Indus Agent sandbox (browser)"
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium xvfb xauth dbus-x11 fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*
ENV INDUS_SANDBOX_CHROMIUM=/usr/bin/chromium
