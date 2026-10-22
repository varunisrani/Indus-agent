# Language servers

When the agent edits a file, a language server can check the edit, the same way your editor would.
If the edit introduced errors, the agent hears about them, in the next tool result or before its
answer is final, and fixes them. Errors that were already in the file are not reported, and
warnings stay quiet.

Indus never installs a language server for you. A server is used when its program is on the `PATH`
(or set in `lsp.servers`); `indus lsp status` shows which are found.

```bash
indus lsp status                  # settings, running servers, and the catalogue with what is installed
indus lsp install-hint pyright    # how to install one
indus lsp restart                 # stop the servers (the gateway's first) and forget their failures
```

`indus lsp restart` asks the profile's running gateway first: the conversations attached to it
(the terminal UI, the desktop app, messaging) run their language servers there, so this is where
a stuck server usually lives. It prints how many servers the gateway stopped, or that it runs none
for this profile; with no gateway running, or one too old to take the request, that part is
skipped. Then it stops the servers of its own process, if it started any. Only that profile's
servers stop: a gateway serving several profiles keeps the others' running. Removing a worktree
(`indus worktree prune`, or the end of an `indus -w` session in the terminal UI) likewise asks
the gateway to stop the servers working in it first.

## Which servers

The catalogue covers Python (pyright or basedpyright), TypeScript and JavaScript, Go (gopls), Rust
(rust-analyzer), C and C++ (clangd), Bash, YAML, Dockerfiles, PHP, Lua, Svelte, Vue, Terraform, Zig,
Gleam, Elixir, Kotlin, Java, OCaml, Haskell, Clojure, Nix and Dart. `indus lsp status` lists them by
id.

Some servers run code the project ships (build scripts, compiler plugins, the project's own
packages): rust-analyzer and gopls, for example. Those start only in a trusted folder: the
session's own git work tree, or a folder listed in `lsp.trusted_workspaces`. The others only read
files and run anywhere. Servers never get your provider keys in their environment.

A server that fails to start is not tried again until `indus lsp restart`, or after
`lsp.broken_retry_s` seconds when you set it. An unused server stops after `lsp.idle_timeout`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `lsp.enabled` | `true` | check edits when a server is installed |
| `lsp.wait_timeout` | `5.0` | seconds to wait for a server's verdict on an edit |
| `lsp.idle_timeout` | `600` | seconds an unused server keeps running (under 30 counts as 30; 0 never stops it) |
| `lsp.broken_retry_s` | `0` | seconds before a failed server is tried again (0: only after `indus lsp restart`) |
| `lsp.trusted_workspaces` | `[]` | extra folders where code-running servers may start |
| `lsp.servers` | `{}` | per-server changes or new servers |

`lsp.servers.<id>` can set `command` (the program and its arguments), `extensions`,
`root_markers`, `language_id`, `env`, `initialization_options` or `disabled: true`. An entry with
`extensions` and an id the catalogue does not know adds a new server, which counts as running
project code:

```yaml
lsp:
  servers:
    pyright:
      disabled: true
    mylang:
      command: [mylang-lsp, --stdio]
      extensions: [.ml2]
```
