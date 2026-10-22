# Task: Security review — threat model and static guards (P25-01) Phase: 25 Owner: wave-43 builder SEC1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `website/docs/user-guide/security.md`
(section headings only, to list the boundaries the reference documents). Our own sources: every
"Security review" paragraph of `docs/plan/phases/LEAD-NOTES.md`, `docs/plan/16-what-we-change.md`
§2.7, the research traps, the w42 README (wiring WB/WC switch on), `tooling/checks/*`.
Evidence: none.

What it does (own words): one document, `docs/internals/threat-model.md`, says what Indus Agent
protects (assets), from whom (actors) and where each trust boundary sits, surface by surface (CLI,
daemon control socket and WebSocket, web dashboard and serve, ACP, MCP serve, gateway adapters,
webhooks, relay and A2A, desktop main/preload/renderer, installers and update, plugins / hub /
packs, egress proxy, browser vault, LSP servers, preview automation). Each boundary row names the
control, the test that proves it (a path that exists; the guard resolves it) and, when something is
wrong or only partly covered, a finding id `SEC-<n>` with severity and status.

Next to it a new guard, `python -m tooling.checks security_static`, reads every Python file under `src/`
and `scripts/` with `ast` (never imports or runs them) and reports the code shapes the review
found risky:

| Rule | Shape | Why |
|---|---|---|
| `shell` | `shell=True` on a `subprocess` call, `asyncio.create_subprocess_shell`, `os.system`, `os.popen` | a string goes through `/bin/sh`; only reviewed places may do it |
| `yaml` | `yaml.load`/`load_all` without a safe loader, `yaml.unsafe_load`, `full_load`, ruamel `YAML(typ="unsafe")` | object construction from data |
| `deserialize` | importing `pickle`, `cPickle`, `marshal`, `shelve`, `dill`, `jsonpickle` | code execution from data |
| `eval` | the builtins `eval` / `exec` | code from strings; only the code-execution sandboxes may |
| `mktemp` | `tempfile.mktemp` | race on the name |
| `archive` | `tarfile` `extractall`/`extract` without `filter=`, `zipfile` `extractall`/`extract` | traversal, links, device files |
| `decompress` | one-shot `zlib` / `gzip` / `bz2` / `lzma` `decompress` | no size bound: a decompression bomb (added after SEC-11) |
| `tls` | `verify=False`, `ssl._create_unverified_context`, `CERT_NONE`, `check_hostname = False` | turns certificate checks off |
| `chmod` | a literal mode with world-write or set-id bits in `chmod`, `os.open`, `mkdir(mode=)`, `umask` | files others can change |
| `xml` | importing `xml.etree`, `xml.dom`, `xml.sax`, `xml.parsers.expat`, `lxml` | entity expansion and external entities; reviewed per file |
| `inherit_env` | a spawn (`subprocess.run/Popen/call/check_call/check_output`, `asyncio.create_subprocess_exec/shell`, `os.exec*`/`os.spawn*`) without an explicit `env=` (src only) | the child inherits the whole process environment: provider keys, tokens |
| `secret_argv` | a spawn whose argv mentions a value named like a secret (`token`, `secret`, `password`, `api_key`, …) | argv is readable by every local user through `/proc` and `ps` |
| `secret_url` | a string or f-string building a URL query with a credential name (`?token=`, `access_token=`, `api_key=`, `key=`, `password=`) | URLs land in logs, history, proxies, Referer |
| `secret_log` | a logging call whose arguments name a secret-named value | secrets in log files |
| `timeout` | `urllib.request.urlopen` without `timeout=`, `requests.<verb>` without `timeout=`, an `httpx` client or call with `timeout=None` | a peer can hold a worker forever |

`os.environ` reads outside the allowed modules stay with the existing `no_raw_environ` guard.

Allow-list: `tooling/checks/security_allow.json`, `{"allow": {"<path>": {"<rule>": "<reason>"}}}`.
An entry without a reason is a violation; an entry that no longer matches anything is a warning (so
the list stays honest, like `no_raw_environ`). Each entry's reason carries the review date.

Inputs → outputs: the tree under `--root` → violations `path:line: [rule] what — why`, warnings for
stale entries, `data.allowed` listing which allow entries were used.

Files: `docs/internals/threat-model.md`, `tooling/checks/security_static.py`,
`tooling/checks/security_allow.json`, `tooling/checks/tests/test_security_static.py`,
`tooling/checks/tests/fixtures/security/` (one seeded file per rule plus a clean file),
`tooling/checks/__init__.py` (register `security`).

Edge cases / failure modes:
- aliases (`import subprocess as sp`, `from subprocess import run`, `from os import system`),
  `**kwargs` on a spawn (cannot see `env=`: counted as passing it, reported only by `inherit_env` when
  no `env` can be inside), `shell=False` and `shell=flag` (only the literal `True` and non-constant
  values that are not `False` count; a variable is reported because it may be true);
- a file that does not parse is a violation, never a silent pass;
- the fixtures folder is skipped by the real run (it is under `tooling/`, which is not scanned);
- secret names are matched on whole identifier words (`token`, `api_key`), not on substrings, so
  `token_count`, `max_tokens`, `tokenizer` are not secrets.

Our design differences / decisions:
- The phase file names the document `docs/internals/security-review.md`; the wave brief names it
  `threat-model.md`. We follow the brief; the phase table's `security-review.md` content is in it.
- The phase file also asks for "no subprocess call outside the env builder's helper". The tree has
  about sixty spawn sites, most correctly passing `env=` from `indus.safety.child_env`; the
  enforceable form of the rule is `inherit_env` (a spawn must say where its environment comes from)
  plus the existing `no_raw_environ` (that environment cannot be a raw copy of the process env).
- "No listener bound without the auth helper": listeners are few and each is reviewed in the
  threat model with its test; a static rule over `uvicorn.Config(host=...)` / `socket.bind` would
  only see literals. Not a static rule; covered by rows and tests (see the document).

Tests we will write: one seeded violation per rule fails with its rule id; lookalikes do not; the
allow-list needs reasons and reports stale entries; an unparsable file fails; the real tree passes;
`security` is registered and runs alone; every threat-model test path resolves (in the P25-02
suite, `tests/security/test_threat_model_doc.py`).

## Decisions for the lead
- Document path `docs/internals/threat-model.md` (wave brief) instead of `security-review.md`
  (phase file); it holds the phase file's boundary table and findings.
- Check name `security_static` (the module is the phase file's `security_static.py`; the runner
  imports a check by its module name), not `security`. CI needs no edit: `tooling.checks
  --require-ref` runs every registered check.
- `inherit_env` instead of "no subprocess outside the env builder" (see above); six reviewed
  files (eight call sites).
- `scripts/` is scanned by every rule except `inherit_env` and `secret_log` (release and parity
  tooling run under a developer's own environment).
- Allow-list reasons must start with their review date (`YYYY-MM-DD: `), enforced by the check.
- Findings SEC-4 (fence laundering through `execute_code` / `delegate_task`) and SEC-5
  (`message_agent` peer answers) need a lead decision; proposals are in the document. SEC-6
  (`isdigit()` → `int()` sweep), SEC-8 (git env for the dashboard update) are follow-ups.
- Proposed D-ids (lead assigns): fence `a2a_call` answers (SEC-3); document zip budget 10,000
  parts / 256 MB (SEC-1); PNG inflation bounded by the header (SEC-11).

Compare checklist (filled after the rewrite; reopened `website/docs/user-guide/security.md` headings):
- [x] approval modes, yolo, hardline floor, deny rules, timeout (rows B-APPROVE-1..4)
- [x] file write safety, protected paths (rows B-FILES-1..3)
- [x] gateway authorization order, allowlists, pairing (rows B-MSG-1..3)
- [x] container isolation, terminal backends (row B-BACKEND-1, B-EGRESS-1)
- [x] env passthrough, credential files, borrowed logins (rows B-ENV-1..3, B-BACKEND-1)
- [x] MCP credential handling, redaction (rows B-MCP-1, B-REDACT-1)
- [x] website policy, SSRF, fake-ip ranges (rows B-NET-1, B-NET-2)
- [x] content-level checks, context file injection (rows B-INJECT-1..3)
- [x] TLS trust (row B-STATIC-1 `tls` rule; email opt-out allow-listed), supply-chain checks
  (rows B-SUPPLY-*), trusted-by-placement extension points (rows B-EXT-*, B-SHELL-1)
- Deliberate differences: our document is organised by surface and boundary, not by feature;
  findings carry ids and tests.

## Status

Done (2026-10-21). `docs/internals/threat-model.md`: 7 assets, 7 actors, 67 boundary rows over
every surface (each with a resolving proof or an owner), 11 findings (SEC-1, SEC-2, SEC-3, SEC-11
fixed with failing-first tests; SEC-4..6, SEC-8 open or follow-up; SEC-7, SEC-9, SEC-10 accepted or
owner-open). Guard `security_static` with 15 rules, 13 reviewed exceptions, 0 violations on the
tree; `tooling/checks/tests/test_security_static.py` (seeded violation per rule, lookalikes,
allow-list rules, real tree). Shared: `tooling/checks/__init__.py` (one name in `NAMES`).

Integration (wave 43, 2026-10-22, integrator): landed on main `bee44d9` (wave 42 `226a5ae` switched
on the surfaces the review anticipated). Re-check against the landed code: 74 rows — B-PREVIEW-1/2,
B-MUX-3 (served-profile attach, D-16.877), B-ROOM-1, B-VOICE-1, B-WEB-9 (admin console), B-WEB-10
(framing) added; B-INJECT-1, B-LSP-1, B-RELAY-1, B-DESK-1..5 and B-SUPPLY-4 updated with the wave-42
and SEC2 proofs — and 18 findings: SEC-5 fixed (`message_agent` fenced, D-16.909); SEC-12
(`drive_preview` text unfenced, D-16.910) and SEC-13 (`file:///` in the desktop preview past the read
guards, D-16.911) found and fixed with failing-first tests; SEC-14 (one language-server tier per
process across served profiles) and SEC-15 (room transcripts not framed) open; SEC-16..18 carry
SEC2's findings. `security_static` on the merged tree: 0 violations (the wave-42 spawns — the LSP
client, room peers — pass `env=`), 13 allow entries used, none stale. D-ids D-16.900, D-16.901,
D-16.912. Done-when (P25): the review still has open findings (SEC-4, SEC-14, SEC-15, SEC-18) that
need a fix or the owner's written acceptance before the release gate.
