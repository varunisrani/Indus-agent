# Task: Backend resolution ladder (P20-08) Phase: 20 Owner: wave-34 builder M

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/main.ts`
(`resolveHermesBackend` and `ensureRuntime`, ~5,005–5,350, read fully; the Python/git discovery
helpers around 2,933–3,370 skimmed). Research 08 §3.3, trap R08.16; D-16.82. Our P23 rows (install
layout: install dir `<home>/indus-agent`, `uv sync` venv, launcher `~/.local/bin/indus`). Closed
before writing.
Evidence: none.

What it does (own words): before the desktop can attach to anything local it needs one `indus`
command it trusts. One resolver walks a fixed ladder and stops at the first rung whose candidate
answers `indus --version` (5 s, exit 0, first line `indus <version>`); a candidate that fails the
probe is logged and skipped, never adopted. The rungs:

1. `INDUS_DESKTOP_INDUS_ROOT` — an absolute path to a developer checkout (it has `pyproject.toml`
   and `src/indus/__init__.py`); its own venv's `indus` is used.
2. unpackaged runs only: the checkout the app runs from (the repo root two levels above
   `apps/desktop`), so Python edits are exercised.
3. `INDUS_DESKTOP_INDUS` — an explicit command (packagers such as Nix); a bare name is looked up on
   PATH because the person named it. Refused when it is this app's own executable, or a Windows
   path while running under WSL.
4. the managed install `<home>/indus-agent` (its venv's `indus`); a usable runtime wins even when
   the bootstrap marker is missing. Skipped by `INDUS_DESKTOP_IGNORE_EXISTING=1`,
   `--ignore-existing` on the app's argv, and while a repair is requested.
5. an install found through the launcher it published in the user bin dir (`~/.local/bin/indus`,
   POSIX): a symlink is followed to its target, a small launcher script is read for the absolute
   path it execs; the target is probed. Skipped like rung 4.
6. nothing usable → a `bootstrap-needed` sentinel (a value, not an error) carrying install hints
   (install dir, platform, packaged).

Never an arbitrary `indus` on PATH and never a system Python. On Windows a venv's `indus.exe`
launcher is replaced by the venv's `python.exe -m indus` (the launcher redirects to the base
interpreter). There is no bundled-payload rung in 1.0 (D-16.82). The resolver awaits the login-shell
PATH merge first (R08.16) — the caller passes the merged env.

Inputs → outputs: `resolveBackend({ env, argv, platform, packaged, home, homeDir, devCheckoutRoot,
appExecPath, isWsl, repair, probe?, fs?, log })` → `{ kind: "found", backend: IndusCommand,
version }` or `{ kind: "bootstrap-needed", hints }`; `IndusCommand { command, args, label, rung,
root }` (args are the prefix before every subcommand, e.g. `["-m", "indus"]`).

Files: `apps/desktop/electron/backend/resolver.ts`, `apps/desktop/electron/backend/resolver.test.ts`,
`apps/desktop/electron/backend/process.ts` (the one bounded child runner the resolver, attach and
installer use; new).

Edge cases / failure modes:
- probe exits non-zero, hangs (killed at 5 s), prints something else → rung skipped, next rung.
- override root relative, missing, or not a checkout → skipped with a log line.
- `INDUS_DESKTOP_INDUS` naming the desktop app itself → skipped (would recurse); a `C:\…` path
  under WSL → skipped.
- launcher a dangling symlink, an unreadable file, a script without an absolute exec path → rung
  skipped.
- `indus` on PATH and nothing else → sentinel (PATH is never a rung).
- ignore-existing / repair → rungs 4 and 5 skipped; rungs 1–3 still apply (the person drives them).

Our design differences / decisions:
- Names: `INDUS_DESKTOP_INDUS_ROOT`, `INDUS_DESKTOP_INDUS`, `INDUS_DESKTOP_IGNORE_EXISTING`.
- No bundled payload rung (D-16.82); no fallback to an older runtime's dashboard command.
- A checkout without a venv is not run through `uv run` (that may sync and download); it is
  skipped with a hint to run `uv sync`.

Tests we will write:
- temp dirs per rung with a fake `indus` (the P20-01 fake backend behind a tiny wrapper): the
  precedence table (each rung wins over every lower one).
- `indus` on PATH only → sentinel; failing and hanging probes skip the rung; ignore-existing skips
  rung 4 (and 5); the app's own binary and a WSL Windows path are refused; launcher symlink and
  launcher script both found; Windows venv unwrap.

## Decisions for the lead
- Managed install location `<home>/indus-agent` and venv `.venv` (from research 10 §install and the
  P23-05 `uv sync`); a P23 change of layout changes `managedInstallDir` / `venvCommand` only.
- Rung 5 exists on POSIX only (no Windows user-bin launcher is specified yet).
- A checkout without `.venv` is skipped, not run with `uv run`.

Compare checklist (filled after the rewrite; reopened `main.ts` 5,005–5,350):
- [x] precedence 1→6, each rung over every lower one (test: `resolver.test.ts::precedence table: each rung wins over every lower one`)
- [x] every candidate trusted only after `--version` (5 s); a failing or hanging probe skips the rung (test: `resolver.test.ts::a failing or hanging probe skips the rung`)
- [x] the app's own binary and a Windows path under WSL are ignored (test: `::refuses the app's own binary and a Windows path under WSL`)
- [x] a usable managed runtime wins without the bootstrap marker (test: `::precedence table …`, rung 4 has no marker)
- [x] ignore-existing (env and argv) and repair skip rungs 4 and 5 only (test: `::ignore-existing and repair skip the installed rungs, not the explicit ones`)
- [x] `indus` on PATH only → sentinel with install hints (tests: `::precedence table …`, `::the sentinel carries install hints`)
- [x] Windows venv launcher unwrapped (test: `::unwraps a Windows venv launcher to the venv's interpreter`); launcher symlink and script (test: `::a launcher script is read for the program it execs`)
- [x] gap found in the integration run: the real CLI prints `Indus Agent 0.0.1` on the first line, not `indus …`; the parser accepts both (test: `resolver.test.ts::parses the version line`, `realBackend.test.ts`)
- Deliberate differences: no bundled-payload rung (D-16.82); a checkout without `.venv` is skipped instead of run through a package manager; the explicit command is probed always (the reference may trust a packager's override unprobed).
- Not taken: CLI-link publishing into `~/.local/bin` (the installer's job), the runtime-gate cache between resolves, the legacy dashboard-command fallback.

## Status

Done (2026-10-21). `electron/backend/resolver.ts` (+ `process.ts`, the bounded runner shared with
P20-09/10). Integration against the real CLI in `electron/backend/realBackend.test.ts`.
