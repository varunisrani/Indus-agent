# Egress proxy

With the Docker terminal backend, the egress proxy keeps your provider keys out of the sandbox. The
container gets a stand-in token instead of each key; a small proxy on your machine swaps the token
for the real key on the way out, only for the hosts that key belongs to.

```bash
indus egress enable        # turn it on for this profile, make its certificate, start the proxy
indus egress status        # on or off, the proxy, its hosts and the key names it fills in
indus egress disable       # turn it off and stop the proxy
```

It needs the `egress` [extra](../getting-started/install.md#what-gets-installed). `enable` starts a background process, so without a terminal it needs
`--yes`.

## What a sandbox gets

Each new container is pointed at the proxy (`HTTPS_PROXY` and friends) and trusts the proxy's own
certificate authority (mounted read-only). Where a real key would be, it gets a token made for that
container alone. The proxy opens the encrypted traffic only for hosts on its list, and there
replaces the token with the real key, read from your secret store at that moment. Traffic to any
other host passes through untouched: the token is useless there. With
`egress.block_other_hosts` on, other hosts are refused instead. Addresses that are not public
(loopback, private networks, cloud metadata) are always refused.

The list starts with the model providers Indus knows: each provider's host and the header its API
reads, for every provider whose key you have set. Add your own:

```bash
indus egress allow api.example.com --secret EXAMPLE_API_KEY            # fills Authorization
indus egress allow api.example.com --secret EXAMPLE_API_KEY --header X-Api-Key
indus egress allow "*.example.com"                                     # open TLS, fill nothing
```

Status output and logs show key names only, never values or tokens.

## Settings that would get around it

With `egress.enforce` on (the default), a sandbox refuses to start when your Docker settings would
bypass the proxy: forwarding or setting a proxy, certificate or filled-in key variable
(`terminal.docker_forward_env`, `terminal.docker_env`), extra `docker run` arguments that set one,
any `--env-file`, or a `--network` flag. With it off, each of those is a warning and your setting
wins. A sandbox with `terminal.docker_network: false` has no network at all and gets nothing from
the proxy.

Containers made with the proxy are never reused without it, and the other way round: after
`enable` or `disable` the next sandbox is a new one.

| Setting | Default | Meaning |
|---|---|---|
| `egress.enabled` | `false` | route Docker sandboxes through the proxy |
| `egress.enforce` | `true` | refuse a sandbox whose settings would bypass the proxy |
| `egress.port` | `47900` | the proxy's port (0 = any) |
| `egress.bind` | `auto` | listen address: the Docker bridge on Linux, else loopback |
| `egress.hosts` | `[]` | extra hosts whose traffic the proxy opens (nothing filled in) |
| `egress.rules` | `[]` | your fill-in rules, `{host, secret, header}` (what `allow --secret` adds) |
| `egress.block_other_hosts` | `false` | refuse hosts that are not on the list |
