## Wave 53 web landing notes (integrator INT-W) — UI parity: w51a foundation + w52 a–f

- Worktree `scratchpad/int-web`, detached at `574689d` (main `5115691` + w51a). Nothing committed; everything is staged
  in the worktree index (`git diff --cached HEAD` is the whole change): 112 paths vs the base (37 added, 75 modified;
  `chat/ModelPickerDialog.tsx` from w52a never reaches the base, so its removal is not a path).
  No package added by any slice (no package.json / lockfile / notices change in a–f).
- **Apply order** `git apply --3way` w52a, b, c, d, e, f (the patch base `0a7f30a` is not in this clone, so every file
  fell back to direct application). Conflicts, each resolved by reading both sides:
  - `ui/Dialog.tsx` (a × c): `description?: ReactNode` (optional, c) rendered only when given, with a's
    `descriptionClassName` slot; a's other class slots and c's `narrow` size kept.
  - `ui/controls.tsx` (b+c × d): Badge tone union; one `secondary` (c's comment; e's `bg-hover` variant dropped for
    `bg-selected`, two of three slices); `CardHeader` (see D-16.1206); `PeriodButtons` + `RefreshButton` (b) kept next to
    d's `EmptyCard`, `CodeChip`, `Callout`; d's duplicate `SectionHeading` dropped (c's kept, it already takes `id`);
    b's `PageSpinner` dropped (d's kept, hidden label); import list union (`RefreshCw`, `RotateCcw`).
  - w52e and w52f each refused once on the unmerged `controls.tsx` (atomic `git apply`); re-applied after staging the
    resolution: e conflicted on Badge `secondary` (above), f on the lucide import and `lib/format.ts` (b's
    `formatTokens`/`formatShortDay` and f's `formatSize`/`formatDateTime` kept side by side).
  - Clean but checked: `e2e/dashboard.spec.ts` (a, e, f), `lib/api.ts` and `testing/mockApi.ts` (c, e), `shell/routes.ts`
    (e title `Profiles/new`, f `/docs` route), `pages/ModelsPage.tsx` (b rewrite + d's `export function DeviceLogin`, kept).
- **Merge notes (README):**
  - One model picker (D-16.1205): `ui/ModelPickerDialog.tsx` = a's look and behaviour (fuzzy filter over providers and
    models with marked letters, provider in use first, CURRENT tags, double-click applies, no-provider sentence with Keys
    / Models links, reset on open, spinner in Switch) on b's contract `onApply({provider, model})`, plus `busy`, `error`,
    `note`; provider meta line keeps b's configured / no key. `chat/ModelPickerDialog.tsx` deleted; `ChatRail` owns the
    `api.model.set` mutation (same invalidations, same "Model set to … Start a new chat" notice). `ModelsPage.test.tsx`:
    reads the CURRENT model row instead of a prefilled field; filter label "Filter providers and models".
  - Dedupes (D-16.1206): Badge `secondary`, `SectionHeading`, `PageSpinner`, `CardHeader` (b's `subtitle` = note after
    the title; `below` = a node, or a string set as d's serif count line with d's list-card padding — the four Keys
    cards now pass `below=`), `Modal` props; `DeviceLogin` export kept.
  - Light warning token → `#ffbd38` (the spec's measured warning, same in both themes) (D-16.1207).
  - Foundation `Button` uppercase (D-16.1208): the specs measure the reference's sm / primary / outlined buttons in
    capitals, ghost buttons mixed case, Keys "Show/Hide advanced" mixed case. `caps` prop (default: every tone but
    `ghost`); `caps={false}` on Show advanced. Accessible names are the DOM text, so no test changed for this
    (vitest and Playwright both green on the same names).
- **Web vitest timeouts, root cause (D-16.1209).** Reproduced on demand: the 7 shell test files on one core with 3
  workers (`taskset -c 0 … --maxWorkers=3`) failed 3/51 twice (App "redirects…" at the 1 s `waitFor`, App "resolves every
  route" and Webhooks "signing secret" at 5 s). CPU profiles (`--cpu-prof`, test window cut by hrtime marks): no
  waiting — the time is React development element creation (owner-stack `Error` per element), react-dom, and jsdom 29's
  `getComputedStyle` cascade (default stylesheet parse, css-color) behind every `*ByRole` query; plus each lazily
  routed page's module graph evaluated inside the first test that visits it (measured 1.7 s for the 18 pages, 0.5 s of
  it SessionsPage + shared deps). "Resolves every route" mounts the full shell 19 times (2.2–2.6 s idle). Fixes, no
  timeout raised: `src/testing/renderApp.tsx` (split from `render.tsx`, used by the 7 shell test files) eagerly loads
  every routed page while the test file is imported (A/B, 3 runs each, one core: redirect 0.56–0.71 s vs 0.84–0.90 s,
  every-route 2.2–2.5 s vs 2.6–3.2 s); the every-route test is now one test per route (`it.each`, 19 tests); f's
  `20_000` test timeout and `findByRole` 10 s on the docs test removed. After: the same 1-core repro failed 1–2 of 69 in
  3 runs (Webhooks "signing secret" 3/3 at 5.1–5.4 s, App redirect 1/3) — that test is 1.3 s of real work idle (render
  0.15, open + fill dialog 0.6, create 0.15, away and back 0.4); under a 4× CPU squeeze it still crosses 5 s. Not
  bumped; follow-up below. Under the gate's conditions (lock, `--maxWorkers=2`) the full suite passed both times it ran
  (342/342 before the test changes, 360/360 on the final source).
- **e2e.** `auth.spec.ts:76` checked: it is not a slice regression but a race the w52a empty state introduced in the
  test's wait — the Sessions page shows the "Recent sessions" card while the list loads and the empty state once a fresh
  stack's empty list arrives, so `getByRole("heading", {name: "Recent sessions"})` passed only when the browser caught
  the loading moment (my runs: :76 passed 1, failed 1; :114 failed 3/3; base `574689d`, which always shows the card:
  3/3 passed). The four such waits (`auth.spec.ts` ×3 incl. :76 and :114, `auth-gates.spec.ts` ×1) wait for "No sessions yet".
- **Gate** (int-web, HOME / INDUS_HOME / caches in the scratchpad, full lanes under `flock fullgate.lock`):
  biome check + format: clean on apps/packages/docs; the repo-wide run reports 52 errors / 2 warnings (format: 7 files)
  only in `.agents/wip/w52/kit-*` (same 52 on the base `574689d`; not product code); `tsc` web 0, web e2e 0,
  packages/ui 0; web vitest `--maxWorkers=2` 40 files, 360 passed (342 before the per-route split, same run conditions);
  web Playwright (fresh `vite build` of web + TUI) 15/15 passed on the final tree (earlier runs before the e2e wait fix: 14/15 twice, :114 failing in the first and :76 in the second); ruff check / format: only `.agents/wip/w52/build_pdf.py`, `kit-web/sheet.py` (24 / 2 files,
  pre-existing on main), product clean; pyright 0 errors 0 warnings; `pytest tests -k "login or auth" -q -p
  no:cacheprovider` 704 passed, 2 skipped, 17822 deselected; `python scripts/release/notices.py --check` fresh (437
  components); `python -m tooling.checks --require-ref` ok (similarity, banned, assets, licenses, … all ok; pins and
  deps_audit warn with 0 issues, as on main), also re-run after the docs edits: ok.
- **Decisions** D-16.1190 – D-16.1209: D-16.1190 the owner's parity decision in §3.2 (decided, 2026-10-24) with a
  pointer at the top of §2.5; §2.7 D-16.1191 – 1192 (W51-a), 1193 – 1194 (W52-sc), 1195 – 1196 (W52-b), 1197 – 1198
  (W52-c), 1199 – 1200 (W52-d), 1201 – 1202 (W52-pc, W52-e), 1203 – 1204 (W52-f), integration 1205 – 1209; §3.1 range
  row. Each slice note gained an "Integration (wave 53)" paragraph naming its D-ids; `web-foundation.md` gained the
  integrated component contract.
- **Not done here (out of INT-W's brief or time):** no re-capture / side-by-side PDF (no reference screenshots in this
  container); LEAD-NOTES.md and HANDOFF.md untouched (lead's).
- **Follow-ups.** (1) Webhooks "signing secret" vitest is 1.3 s idle and can still cross 5 s under a 4× CPU squeeze:
  split create / away-and-back if parallel builders keep running full suites, or run full web vitest only through the
  lock. (2) Biome and ruff run over `.agents/wip/w52/kit-*` and `build_pdf.py` (pre-existing failures on main): exclude
  `.agents/` in `biome.json` / ruff config or fix the kits. (3) The light warning `#ffbd38` is low-contrast as small
  text on the light canvas (parity choice, D-16.1207). (4) Backend follow-ups carried from the slices: usage
  `api_calls` / `by_model`, per-provider model list, skills write endpoint, MCP OAuth client calls, sessions import.
- Environment: every uv / node call ran with the scratchpad HOME and caches; TMPDIR unset for the web e2e (its daemon
  socket path); no /tmp/indus-e2e-* touched by hand; only processes this run started; a second worktree
  `scratchpad/int-web-base` (574689d, node_modules hard-linked, `apps/web/dist` and `apps/tui/dist` built) was made to
  check `auth.spec.ts:76` on the base — can be removed with `git worktree remove`.
