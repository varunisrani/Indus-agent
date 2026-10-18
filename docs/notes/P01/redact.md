# Task: Redaction core (P01-17) Phase: 01 Owner: P01 S-env builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/redact.py` lines 88–260, for the
categories of patterns only (vendor key prefixes, assignment forms, query params, the import-time
switch snapshot). Research `04-cli-config.md` R04.13, §3.17 masking. Closed before writing; every
regex and every test case here is our own corpus.
Evidence: none.

What it does (own words): `Redactor(scope)` masks secrets in text and in nested objects before they
reach a log file, a transcript or a spill file. Two layers: shape patterns for credentials that look
like credentials (vendor prefixes, bearer headers, JWTs, PEM private keys, URL passwords and
sensitive query params, `NAME=value` / `"name": "value"` where the name is credential-shaped), and
exact values the profile actually holds (every `SecretStore` value and every secret-looking `.env`
value, ≥ 8 chars, longest first). Each pattern reports the span of the secret part; spans are merged
and each merged span is masked once, so nothing is masked twice. The on/off switch is read from the
scope on every call (no import-time snapshot, R04.13 by design); `force=True` ignores it.

Inputs → outputs: `Redactor(scope)`, `.redact(text, *, force=False) -> str`,
`.redact_obj(obj, *, force=False)` (new object; dict/list/tuple/set; depth cap 64 → `"[depth limit]"`,
cycles → `"[cycle]"`), `.enabled`; `mask(value)` (`<first 4>…<last 4>` for ≥ 12 chars, else
`[redacted]`); `redact_text(text, secrets=())` for callers without a scope.

Files: `src/indus/foundation/redact.py`, `tests/unit/foundation/test_redact.py`.

Edge cases / failure modes:
- Kept: `${VAR}` / `$VAR` references, paths (`/…`, `~/…`, `./…`), `os.environ[...]` / `os.getenv(`
  lookups, `None`/`null`/`true`/`false`, names where the keyword is only part of a word (`MONKEY`,
  `AUTHOR`, `KEYBOARD`).
- URL with both a password and a `token=` → two masks.
- `.env` values that are plainly not secrets (numbers, booleans, paths, URLs without userinfo) are
  not added to the exact-value list, so a base URL does not vanish from every log line.
- Exact-value list cached on (`.env` stat signature, store generation).
- PEM blocks are found by a BEGIN/END scan that stops at the first unterminated BEGIN (linear time;
  a regex with a lazy body went quadratic on repeated BEGIN lines).
- A guard pass replaces any stored value still present after masking (a mask never re-forms one).
- Store or `.env` read errors never break redaction: patterns still run, error logged at DEBUG.

Our design differences / decisions:
- Span merging instead of sequential substitutions.
- The switch comes from the scope env (`INDUS_REDACT_SECRETS`, bridged from config by P01-15),
  read at call time; a disabled switch still allows `force=True`.
- Smaller prefix list than the reference (the families the spec names plus a few common ones);
  exact-value redaction covers the rest for keys the profile holds.

Tests we will write: ≥ 40-row positive/negative table across the categories; URL masked twice;
store secret in free text; disabled switch; force; `redact_obj` nesting, keys, cycles, depth cap;
mask format; seeded-random property: output never contains a stored value.

## Decisions for the lead
- Exact-value redaction skips `.env` values that are numbers, booleans, paths or plain URLs (the spec
  says "every value"; masking base URLs and paths everywhere made logs useless). Remove
  `_looks_public` to follow the letter.
- hypothesis not in the dev group: property test is a seeded random loop.

Compare checklist (filled after the rewrite; reopened `agent/redact.py` 88–260):
- [x] vendor prefixes, assignment forms, query params covered (table in `tests/unit/foundation/test_redact.py::test_table`)
- [x] switch not snapshotted at import (test: `::test_switch_read_per_call`)
- Deliberate differences: per-call switch from the scope; exact-value layer; span merge.
- Not taken: tool-output use and the plugin pattern hook (P05).

## Status

Done (2026-10-18).

## Integration (lead)

- `scope.SecretSource` gained `known_values()` and `generation` (what `SecretStore` has), so a real
  `Scope` satisfies `RedactScope` (fixed the 3 pyright errors in `log.py`). `redact.py` unchanged.
- Tests use the real modules; their local `FakeScope` stays (a duck-typed scope, not a stand-in).
