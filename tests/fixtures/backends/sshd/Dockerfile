# A throwaway sshd for the SSH live lane (tests/integration/backends/test_ssh_live.py, P11-21).
# Key-only login for one user; the public key comes in at run time through AUTHORIZED_KEY.
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server bash procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash indus \
    && mkdir -p /run/sshd /home/indus/.ssh \
    && chmod 700 /home/indus/.ssh \
    && chown indus:indus /home/indus/.ssh \
    && printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
        'PermitRootLogin no' 'AcceptEnv LIVE_*' > /etc/ssh/sshd_config.d/indus.conf
EXPOSE 22
CMD ["/bin/sh", "-c", "printf '%s\\n' \"$AUTHORIZED_KEY\" > /home/indus/.ssh/authorized_keys && chown indus:indus /home/indus/.ssh/authorized_keys && chmod 600 /home/indus/.ssh/authorized_keys && ssh-keygen -A >/dev/null && exec /usr/sbin/sshd -D -e"]
