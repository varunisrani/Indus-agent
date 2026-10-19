---
name: project-onboarding
description: Map an unfamiliar repository before changing it
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: software-development
    tags: [codebase, architecture, onboarding, exploration]
---

# Project onboarding

Before changing code you have not seen, spend a few minutes building a map: what the project is,
how it is laid out, how to run it and its tests, and which rules its maintainers expect you to
follow. The map pays for itself on the first change.

## When to use

- The user points you at a repository or folder you have not worked in this session.
- The user asks "how does this project work?" or "where is X handled?".
- A task touches a part of a known project you have not read yet.

## Procedure

1. **Read the guidance files first.** With `read_file`, in this order when they exist:
   `AGENTS.md`, `CLAUDE.md` and similar agent instructions, `README*`, `CONTRIBUTING*`,
   `docs/` index pages. Rules found here override your defaults.
2. **Get the shape.** List the top two levels of the tree (`search_files` by name, or
   `terminal`: `git ls-files | head -200`). Note source, tests, docs, scripts, config.
3. **Identify the stack.** Read the manifest files: `pyproject.toml`, `package.json`, `go.mod`,
   `Cargo.toml`, `pom.xml`, `Makefile`, `Dockerfile`, CI workflow files. Write down the language,
   framework, package manager and the commands for build, test, lint and run.
4. **Find the entry points.** The `main`, the CLI definition, the HTTP routes or the exported
   package API. Follow one real request or command from entry to output with `read_file` and
   `search_files`.
5. **Check it runs.** Install dependencies and run the test suite (or its fastest subset) through
   `terminal`. Note failures that exist before you touch anything, so you never blame yourself
   for them later, or miss one you caused.
6. **Write the map.** Give the user (or keep for yourself in `todo_list`) a short summary: purpose,
   layout, key modules, how to build and test, conventions, open questions.
7. **Remember durable facts.** If the user will come back to this project, offer to save the build
   and test commands with the `memory` tool.

## Pitfalls

- Reading every file. Skim structure first; read deeply only on the path your task needs.
- Trusting stale docs over the code and CI config. When they disagree, the CI config is usually
  what actually runs.
- Running install scripts you have not read. Check `postinstall` hooks and `curl | sh` steps
  before executing them.
- Changing formatting or layout to your taste in a project with its own conventions.

## Verification

- You can name the command that runs the tests, and you have run it.
- You can point to the file where the feature you are about to change lives.
- Your summary answers "how do I build, test and run this?" in a few lines.
