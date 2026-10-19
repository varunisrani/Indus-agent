Payloads for the skills hub tests (P13-25, P13-26). Tests serve them from a fake web (httpx
MockTransport behind the SSRF-safe transport) or a local HTTP server; nothing here is fetched from
the internet.

- `repo/`: the files of a fake GitHub repo `acme/agent-skills`; `github_repo.json` and
  `github_tree.json` are what the GitHub API answers for it (tree sha `5f1e0c7`).
  `skills/sql-helper/scripts/unused.sh` is in the repo but not referenced by `SKILL.md`, so an
  install must not copy it. `.hidden` and `_drafts` are skipped by tap listings.
- `well_known/`: a site's `/.well-known/skills/` folder with its `index.json`.
- `skills_sh_search.json`: what the skills.sh search API answers for `sql`.
