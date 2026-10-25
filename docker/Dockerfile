# Indus Agent product image (P23-08).
#
#   docker build -f docker/Dockerfile -t indus-agent:local .
#   docker build -f docker/Dockerfile --build-arg VARIANT=browser -t indus-agent:local-browser .
#
# Code and its interpreter live in /opt/indus (root-owned, never written at run time); everything a
# user changes lives in the /opt/data volume (INDUS_HOME). s6-overlay is PID 1: it reaps stray
# children, runs the optional gateway/dashboard services and then the container's command as its
# main program (docker/entrypoint.sh, docker/main-wrapper.sh).
#
# Every download is pinned: the base image by digest (BASE_IMAGE below, rendered from
# docker/pins.json by `scripts/docker/pins.py render`), uv by the installer's pins, s6-overlay and
# Node by docker/pins.json, all verified by SHA-256 before use.

ARG BASE_IMAGE=debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
ARG VARIANT=slim

# ---- fetch: pinned downloads, verified before anything is unpacked ----------------------------
FROM ${BASE_IMAGE} AS fetch
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 xz-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY docker/pins.json docker/pins.json
COPY scripts/install/pins.json scripts/install/pins.json
COPY scripts/docker/pins.py scripts/docker/pins.py
RUN python3 -I scripts/docker/pins.py --root /src fetch uv --arch "${TARGETARCH:-amd64}" --out /dl/uv \
    && python3 -I scripts/docker/pins.py --root /src fetch s6-overlay --arch "${TARGETARCH:-amd64}" --out /dl/s6 \
    && python3 -I scripts/docker/pins.py --root /src fetch node --arch "${TARGETARCH:-amd64}" --out /dl/node \
    && mkdir -p /out/uv /out/s6root /out/node \
    && tar -xzf /dl/uv/*.tar.gz -C /out/uv --strip-components=1 \
    && for part in /dl/s6/s6-overlay-noarch.tar.xz /dl/s6/s6-overlay-symlinks-noarch.tar.xz; do \
         tar -C /out/s6root -xJpf "$part"; done \
    && for part in /dl/s6/s6-overlay-x86_64.tar.xz /dl/s6/s6-overlay-aarch64.tar.xz; do \
         if [ -f "$part" ]; then tar -C /out/s6root -xJpf "$part"; fi; done \
    && tar -xJf /dl/node/*.tar.xz -C /out/node --strip-components=1 \
    && test -x /out/uv/uv && test -x /out/s6root/init && test -x /out/node/bin/node

# ---- web: the dashboard pages, built with the pinned Node -----------------------------------
FROM ${BASE_IMAGE} AS web
COPY --from=fetch /out/node /opt/node
ENV PATH=/opt/node/bin:${PATH} \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    CI=1
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY apps/ apps/
COPY packages/ packages/
# The in-app Docs page bundles the user guides and their navigation (W58-J2-1).
COPY mkdocs.yml ./
COPY docs/user/ docs/user/
RUN corepack enable \
    && pnpm install --frozen-lockfile --filter "@indus/web..." \
    && pnpm --filter @indus/web build \
    && test -f apps/web/dist/index.html

# ---- build: Python, the locked environment and the package --------------------------------
FROM ${BASE_IMAGE} AS build
ARG VARIANT
ARG PYTHON_VERSION=3.14
ARG INDUS_EXTRAS="web docs pty dashboard acp messaging"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /out/uv/uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/indus/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PROJECT_ENVIRONMENT=/opt/indus/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CONFIG=1 \
    UV_CACHE_DIR=/tmp/uv-cache
WORKDIR /opt/indus
RUN uv python install "${PYTHON_VERSION}"
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
# Dependencies first (cached until the lock changes), then the project itself (editable, so the
# no-wheel build guard of P23-01 is not involved). The extras list is split into words on purpose.
# hadolint ignore=SC2086
RUN set -eu; \
    extras="${INDUS_EXTRAS}"; \
    if [ "${VARIANT}" = browser ]; then extras="${extras} browser"; fi; \
    set --; for e in ${extras}; do set -- "$@" --extra "$e"; done; \
    uv sync --frozen --no-dev --no-install-project --python "${PYTHON_VERSION}" "$@"
COPY src/ src/
COPY skills/ skills/
COPY locales/ locales/
# pyproject's build hooks (P23-01) live under scripts/.
COPY scripts/ scripts/
# hadolint ignore=SC2086
RUN set -eu; \
    extras="${INDUS_EXTRAS}"; \
    if [ "${VARIANT}" = browser ]; then extras="${extras} browser"; fi; \
    set --; for e in ${extras}; do set -- "$@" --extra "$e"; done; \
    uv sync --frozen --no-dev --python "${PYTHON_VERSION}" "$@" \
    && rm -rf /tmp/uv-cache
COPY --from=web /src/apps/web/dist /tmp/web-dist
RUN /opt/indus/.venv/bin/python -m indus.web.bundle publish --source /tmp/web-dist \
    && rm -rf /tmp/web-dist \
    && printf 'docker\n' > /opt/indus/.install_method \
    && /opt/indus/.venv/bin/indus --version

# ---- the image -----------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS product
ARG VARIANT
LABEL org.opencontainers.image.title="Indus Agent" \
      org.opencontainers.image.description="Indus Agent: CLI, gateway and dashboard" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/indusagi/indus-agent"
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client procps ripgrep xz-utils less; \
    if [ "${VARIANT}" = browser ]; then \
        apt-get install -y --no-install-recommends chromium fonts-liberation fonts-noto-color-emoji; \
    elif [ "${VARIANT}" != slim ]; then \
        echo "unknown VARIANT ${VARIANT} (slim or browser)" >&2; exit 1; \
    fi; \
    rm -rf /var/lib/apt/lists/*
COPY --from=fetch /out/s6root/ /
COPY --from=build /opt/indus /opt/indus
COPY --chmod=0755 docker/entrypoint.sh docker/stage2.sh docker/main-wrapper.sh /opt/indus/docker/
COPY --chmod=0755 docker/s6/ /etc/s6-overlay/s6-rc.d/
RUN groupadd --gid 10000 indus \
    && useradd --uid 10000 --gid indus --home-dir /opt/data --no-create-home --shell /bin/bash indus \
    && mkdir -p /opt/data \
    && chown indus:indus /opt/data \
    && chmod -R go-w /opt/indus \
    && if [ "${VARIANT}" = browser ]; then printf '/usr/bin/chromium\n' > /opt/indus/.image_chromium; fi
ENV INDUS_HOME=/opt/data \
    INDUS_IMAGE_VARIANT=${VARIANT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/indus/.venv/bin:${PATH} \
    S6_KEEP_ENV=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0
WORKDIR /opt/indus
VOLUME ["/opt/data"]
ENTRYPOINT ["/opt/indus/docker/entrypoint.sh"]
CMD []
