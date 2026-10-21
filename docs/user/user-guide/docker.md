# Docker

The image `ghcr.io/indusagi/indus-agent` runs Indus Agent in a container: typically the messaging
gateway and the dashboard, sharing one data folder with your machine. Tags: `stable` (the newest
release), `canary`, and each version; `-browser` variants include Chromium for the browser tools.

Inside the container the data folder is `/opt/data`; the compose files mount your `~/.indus` there
(`INDUS_DATA_DIR` picks another folder). The container's user takes the ids in `INDUS_UID` and
`INDUS_GID`, so the files it writes stay yours.

## Linux

```bash
INDUS_UID="$(id -u)" INDUS_GID="$(id -g)" docker compose -f docker/compose.yaml up -d
```

This starts two services from the image: `gateway` (`indus gateway run`) and `dashboard`. The
compose file uses host networking, and the dashboard listens on `127.0.0.1:9119` of the host only,
so it needs no login. To reach it from another computer, use an SSH tunnel:

```bash
ssh -L 9119:127.0.0.1:9119 your-server
```

Run any `indus` command inside the image the same way, for example the first setup:

```bash
docker compose -f docker/compose.yaml run --rm -it gateway setup
```

## Windows (Docker Desktop)

Docker Desktop has no host networking. The dashboard therefore listens on the container's own
network interface, and Docker publishes it on `127.0.0.1:9119` of your machine. Because that
interface is not loopback inside the container, **the dashboard starts only after a login provider
is configured**. There is deliberately no option that turns the login off.

Set one up before the first `up`, using the settings on the shared data folder:

1. Username and password. Run the dashboard once with a terminal attached; it offers to create the
   login and stores only a hash of the password:

    ```powershell
    docker compose -f docker/compose.windows.yaml run --rm -it dashboard
    ```

    Answer the questions, then stop it with Ctrl+C. Alternatively put
    `INDUS_DASHBOARD_USERNAME` and `INDUS_DASHBOARD_PASSWORD` in the profile's `.env` file in the
    data folder.

2. Or single sign-on with your identity provider (OIDC, public client):

    ```powershell
    docker compose -f docker/compose.windows.yaml run --rm gateway config set dashboard.oidc.issuer https://login.example.com
    docker compose -f docker/compose.windows.yaml run --rm gateway config set dashboard.oidc.client_id indus-dashboard
    ```

Then start both services:

```powershell
docker compose -f docker/compose.windows.yaml up -d
```

The data folder is `%USERPROFILE%\.indus` unless `INDUS_DATA_DIR` says otherwise.

## Commands in a sandbox container

Separately from running Indus Agent itself in Docker, the agent can run its *commands* in a Docker
container (`terminal.backend: docker`). That uses the sandbox image
`ghcr.io/indusagi/indus-agent-sandbox`; see [Tools and backends](tools-and-backends.md).

## Updating

```bash
docker compose -f docker/compose.yaml pull
docker compose -f docker/compose.yaml up -d
```

`indus update` does not run inside the image; a new image is the update. Set `INDUS_IMAGE` to pin
an exact version or digest.
