Sample agent homes for `indus import-agent` tests (P24-18), written by us.

- `claude_code_home/` is a user home holding `.claude/` and `.claude.json`.
- `codex_home/` is a user home holding `.codex/`.

No value here is a credential. Tests that need a token-shaped value write it at run time. The
credential files (`.claude/.credentials.json`, `.codex/auth.json`) hold a
plain note: the importer must never open them (a skill's `.env` is written at run time:
`.env` files are git-ignored).
