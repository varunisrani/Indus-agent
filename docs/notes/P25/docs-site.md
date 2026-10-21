# Task: Docs site build (P25-14) Phase: 25 Owner: builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `website/docusaurus.config.ts`
(structure only: which plugins and presets, broken-link policy, search choice),
`website/sidebars.ts` (top-level section list only), `website/scripts/prebuild.mjs` (which files are
generated before a build: a skills index, `llms.txt`, `llms-full.txt`, blueprint and plugin data).
Evidence: none.

What it does (own words): one command turns the Markdown under `docs/user/` into a static site with
a left navigation, built-in full-text search and two plain-text files for language models:
`llms.txt` (a short index: one line per page with its title, link and one-line summary, grouped by
navigation section) and `llms-full.txt` (every page's Markdown in navigation order). The build runs
in strict mode, so a link to a page or anchor that does not exist, a page left out of the
navigation, or a navigation entry without a page fails the build instead of printing a warning. A
workflow builds the site on every push to `main` that touches the docs and on every published
release, and deploys it to GitHub Pages; CI builds it (no deploy) on every change.

Inputs → outputs: `docs/user/**/*.md`, `mkdocs.yml` → `build/docs-site/` (gitignored) with
`index.html` per page, `search/search_index.json`, `llms.txt`, `llms-full.txt`.
`uv run mkdocs build --strict` (strict is also set in the config file, so a plain build is strict).

Files: `mkdocs.yml`, `scripts/docs/llms_txt.py` (an MkDocs hook module plus plain functions),
`.github/workflows/docs.yml`, `tests/unit/docs/test_docs_build.py`; `pyproject.toml` dev group
(`mkdocs`, `mkdocs-material`, exact pins); `.github/workflows/ci.yml` (strict build step).

Edge cases / failure modes:
- a page in `docs/user/` that the navigation does not list: strict fails (`validation.nav.omitted_files`).
- a relative link to a missing page or a missing `#anchor`: strict fails (`validation.links`).
- a page without a top-level heading: `llms.txt` falls back to the navigation title.
- pages outside the navigation never reach `llms.txt` silently: the hook raises when the site has a
  page it did not list.
- llms files must not depend on the build machine: no timestamps, no absolute paths; links are
  relative to `site_url` when set, else site-relative.
- MkDocs 2.0 pre-releases exist on PyPI: the pin stays on 1.6.1 (Material 9.7.7 requires `<2`).

Our design differences / decisions:
- Site generator: MkDocs 1.6.1 with Material 9.7.7 (the phase plan names MkDocs + Material); the
  reference uses a JavaScript site generator with a hosted search service. Our search is the
  theme's built-in client-side index: no third-party service, no key in the repo.
- The llms files are produced by an MkDocs hook at build time (no separate pre-build step and no
  second script entry); the same module exposes `render_index` / `render_full` for tests.
- No translations in 1.0 (16 §2.1 "Docs website" row): English only.
- The site has no plugin catalogue, skills hub page or blueprint gallery: those pages in the
  reference are generated from its own catalogues; ours are the generated reference pages (P25-15).

Tests we will write:
- strict build of the real tree is green; the site has every page and the search index.
- `llms.txt` lists every page of the navigation (and every Markdown file in `docs/user/`);
  `llms-full.txt` contains every page's text in navigation order.
- a broken link in a scratch copy of the docs fails the strict build; an omitted page fails it.
- the built text (`llms-full.txt`) is clean under the similarity guard's prose detector against the
  reference (marked `ref`).

## Decisions for the lead
- MkDocs 1.6.1 (BSD-2-Clause, 2024-08-30) and mkdocs-material 9.7.7 (MIT, 2026-07-17) added to the
  `dev` group only; `uv.lock` left to the lead (re-lock online). Transitive packages listed in the
  report. Change: swap the pins in `pyproject.toml` and `mkdocs.yml` keeps working (no
  Material-only Markdown extensions beyond `admonition`, `pymdownx.superfences`, `toc`).
- Pages deploy uses the `github-pages` environment and the three official Pages actions at their
  major tags (`configure-pages@v5`, `upload-pages-artifact@v4`, `deploy-pages@v4`); SHA-pinning
  them is a follow-up with the other unpinned actions in ci.yml.
- `site_url` is left unset until the owner picks the docs domain (P25-20 checklist); llms links
  are then site-relative.

Compare checklist (filled after the rewrite; reopened `website/docusaurus.config.ts`,
`website/sidebars.ts`, `website/scripts/prebuild.mjs`):
- [x] llms index + full text generated at build (test: `tests/unit/docs/test_docs_build.py::test_llms_index_lists_every_page`)
- [x] search built in (test: `tests/unit/docs/test_docs_build.py::test_strict_build_of_the_real_tree`)
- [x] the reference's top-level sections (getting started, using it, features, messaging, integrations, guides, developer guide, reference) each have a place in our navigation or a recorded reason (see `docs-guide.md`)
- Deliberate differences: the reference builds with broken links as warnings; ours fails the build on any broken link or anchor (strict). The reference's search is a hosted service; ours is local.
- Not taken: translations, the plugin/skill catalogue pages, a desktop-download landing page.

## Status

Done (2026-10-21). See the final report for the check results.
