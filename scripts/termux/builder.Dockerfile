# Termux builder image for the APT channel (P24-21), used by scripts/termux/build.sh.
#
#   docker build --platform linux/arm64 -f scripts/termux/builder.Dockerfile scripts/termux
#
# The base is a bionic (Android) aarch64 root file system, pinned by digest below; build.sh reads
# the digest from this ARG line and refuses to build while it is the all-zero placeholder.
# OWNER ITEM (owner-to-fill): resolve the digest once, with network, e.g.
#   docker buildx imagetools inspect docker.io/termux/termux-docker:aarch64
# and replace the zeros. `python -m tooling.checks pins` reports the placeholder until then.
ARG BASE_IMAGE=docker.io/termux/termux-docker:aarch64@sha256:0000000000000000000000000000000000000000000000000000000000000000

FROM ${BASE_IMAGE}

# The Termux tree belongs to uid 1000 and Termux's apt will not run as root, so the image works
# as that user. There is no /bin/sh in the base; every RUN goes through Termux's bash.
USER 1000:1000
SHELL ["/data/data/com.termux/files/usr/bin/bash", "-c"]
ENV PREFIX=/data/data/com.termux/files/usr
ENV PATH=/data/data/com.termux/files/usr/bin:/usr/bin:/bin
ENV DEBIAN_FRONTEND=noninteractive

# Runtimes the .deb ships (python, nodejs-lts, uv: Termux's own bionic builds) and the native
# toolchain the wheel stage needs for packages that publish no Android wheel (libc++ named so apt
# moves it to the version its cmake needs; the mirror's index is retried once while it syncs).
# `wheel` retags the built wheels with the PEP 738 platform tag; its version is pinned here.
RUN printf 'deb https://packages.termux.dev/apt/termux-main stable main\n' \
        > "$PREFIX/etc/apt/sources.list" \
 && (apt-get update || apt-get update) \
 && apt-get install -y --no-install-recommends \
        python python-pip nodejs-lts uv \
        libc++ clang rust make cmake ninja pkg-config patchelf binutils \
        libffi openssl zlib libsqlite liblzma libbz2 libyaml \
 && python3 -m pip install --no-cache-dir wheel==0.45.1 \
 && apt-get clean
