---
name: shell-scripting
description: Write safe, repeatable shell scripts
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: software-development
    tags: [bash, shell, scripts, automation]
    requires_toolsets: [terminal]
---

# Shell scripting

A good script can run twice without harm, stops at the first real error, and says what it is
doing. Aim for that even in a ten-line helper.

## When to use

- The user wants a task automated as a script they can keep and re-run.
- A one-off command grows past a single line or needs to loop over files.
- You are fixing or reviewing an existing shell script.

## Procedure

1. **Pick the right tool.** Shell is good at running other programs and moving files. For parsing
   JSON, doing arithmetic on data or anything over about a hundred lines, suggest Python (run it
   with `execute_code` or a `.py` file) instead.
2. **Start from a strict header.**
   ```bash
   #!/usr/bin/env bash
   set -Eeuo pipefail
   IFS=$'\n\t'
   ```
   `-e` stops on errors, `-u` catches unset variables, `pipefail` keeps a failing stage of a pipe
   from being masked.
3. **Quote every expansion**: `"$file"`, `"${items[@]}"`, `"$(command)"`. Unquoted variables split
   on spaces and expand globs.
4. **Make it idempotent.** Check before you create: `mkdir -p`, `ln -sfn`, `grep -q … || echo … >>`,
   test for a file before downloading it again. A second run should change nothing.
5. **Handle the edges.** Give arguments defaults or a usage message; validate them early. Use
   `mktemp -d` for scratch space and clean it with `trap 'rm -rf "$tmp"' EXIT`.
6. **Make destructive steps explicit.** Print what will be deleted or overwritten; add a
   `--dry-run` flag for anything that touches many files.
7. **Write it with `write_file`, mark it executable** (`chmod +x`), and lint it:
   `shellcheck script.sh` through `terminal` when shellcheck is installed.
8. **Try it** on a small, disposable input first, then on the real one.

## Pitfalls

- `cd somewhere && rm -rf *` style lines: if `cd` fails, the delete runs in the wrong place. With
  `set -e` and a quoted, checked path this is avoided; still prefer `rm -rf -- "$dir"/*` with
  `$dir` validated.
- Parsing `ls` output. Use globs or `find … -print0 | xargs -0`.
- `$?` read after another command has already replaced it.
- macOS ships older bash and BSD tools (`sed -i ''`, no `readlink -f`). Say which platforms the
  script supports.
- Secrets passed as command-line arguments show up in process lists. Read them from the
  environment or a file with tight permissions.

## Verification

- `bash -n script.sh` (syntax) and `shellcheck` pass, or every warning is understood.
- Running the script twice in a row gives the same end state and no errors.
- A bad argument prints usage and exits non-zero.
