# 01 · Clean-room protocol

How Indus Agent gets the reference's behaviour without any of its code, and how we can show that
later. The short form for day-to-day work is in `AGENTS.md`.

## Variant

**Read → Rewrite → Compare**, decided by the lead on 2026-10-18.

The reference is MIT-licensed, so a strict two-team separation (one side reads, the other never
does) is not needed and would slow the build badly. Instead a builder may read the reference, but
never writes while it is open, and opens it again afterwards only to look for gaps. Automated
guards in `tooling/checks/` back this up on every change.

The reference: hermes-agent at `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`, checked out at
`/root/reference/hermes-agent` (outside the repo, read-only; `$RUNNER_TEMP/ref` in CI).

## The loop

1. **Read** — the task's reference files: documentation for the *why*, code for the *what*, tests
   for the edge cases. Also the area spec and any Evidence for the task.
2. **Note** — the task note (`docs/notes/<phase>/<slug>.md`), in our words, before any code.
3. **Rewrite** — reference closed. Build from the note and the plan with our names and our own
   decomposition.
4. **Compare** — reopen the reference, go through it branch by branch, close each gap by writing it
   again ourselves, and tick it in the note.
5. **Check** — tests, lint, `uv run python -m tooling.checks --require-ref`; for UI work also a
   parity capture. Any failure returns to step 3.

A useful test of step 3: if the reference vanished from disk, could you still justify every line
you wrote? If a block only makes sense next to theirs, write it again.

## What may be read, and when

| Source | Read | Rewrite | Compare | Allowed in the repo |
|---|---|---|---|---|
| Reference code, docs and tests | yes | no | yes | never |
| REA Evidence (decompiled code, bundles, captures, DOM, screenshots, DB dumps) | yes | no | yes | never; cite the `evidence_id` only |
| The running reference in a sandbox | yes | no | yes | never; captures stay in `~/rea-lab/` |
| Our area specs and plan | yes | yes | yes | yes |
| The task note | written | yes | updated | yes |
| Third-party protocol and vendor API docs | yes | yes | yes | names only, spelled as the vendor does |

Evidence counts as reference code: it often contains the reference's own text, so it is treated
exactly like the source.

## Allowed and not allowed

Allowed:
- Reading anything during Read and Compare.
- Matching behaviour: the same algorithms, state machines, limits and timings.
- Using the same open-source libraries.
- Writing our own tests for the same edge cases.
- Third-party protocol names, spelled exactly.
- A module split like theirs where the concept itself forces it.

Not allowed:
- Copying of any size, including copy-then-rename.
- Writing with the reference open beside the editor.
- Its comments, documentation text, prompts, or UI copy beyond a short label of one to four words.
- Its tests, fixtures, snapshots, schemas, config files or themes.
- Its assets: logos, icons, sounds, fonts, screenshots.
- Its names in product code. Ours are Indus Agent, `indus`, `INDUS_`, `~/.indus`; the guard keeps a
  list of banned identifiers.

Models remember public code. Not having opened a file is no defence; the similarity guard applies
to everything.

## Guards

`uv run python -m tooling.checks` runs all of them, keeps going after a failure, and exits with the
worst result: 0 clean, 1 violations, 2 setup problem. `--require-ref` turns a missing reference
into exit 2 instead of a skip; CI and releases always pass it. The guards cover code and prose
similarity against the pinned reference, banned identifiers, identical asset files, dependency
licences, and (in CI) the PR body's `Reference:` and `Task note:` lines.

## When the similarity guard hits

A hit says "this region is not yet our own writing". The change waits until it is.

1. Look at both sides: our file and lines, the reference file and lines, the shared run.
2. Prose (a comment or doc line): do not reword the sentence. Reread our code and describe it again
   from nothing.
3. Code: close the reference, reread the task note, and rebuild the region with a different shape,
   such as another split, a named helper, or another order of checks. Renaming does not count.
4. Rerun the guard and the module's tests; behaviour must not change.
5. Allow-list only overlap that is genuinely forced: generated output, text fixed by a protocol or
   platform, or licence text we are required to reproduce. Each entry needs a written reason.
6. Record the region and the verdict (idiom, platform, contract, too close) in the similarity
   report under `docs/internals/`. "Too close" is rewritten before merge.

## Credits

- `THIRD_PARTY_NOTICES.md` holds the reference's MIT licence text and copyright line, says that
  Indus Agent is written from scratch, and points to the guards that check it.
- `README.md` and, later, the About screen carry the credit line.
- Dependency notices are generated from installed packages at build time.
- We never use the reference's name, logo, icons, colours or screenshots as our own.
- The product's own licence is the owner's decision and is not set here.
