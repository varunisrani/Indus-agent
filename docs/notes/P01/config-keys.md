# Task: dotted keys and value coercion (P01-13) Phase: 01 Owner: S-config builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config.py` (nested key
helpers: split, greedy literal match, phantom sibling, set/get/unset; `set_config_value` and its
coercion helpers and container table), `hermes_cli/config_section_guard.py`. Closed before writing.
Evidence: none.

What it does (own words): config keys are dotted paths, but real key names contain dots (model ids
like `grok-4.6`). `split_key` honours `\.` escapes; navigation prefers an existing literal key that
equals the dot-join of the next segments (longest wins), so `providers.p.models.grok-4.6.x` finds
the real `grok-4.6` entry; a write that would create a new mapping `grok-4` next to an existing
`grok-4.6` is refused because the runtime would never read it (R04.8). `coerce` turns the text a
user typed into a value with the slot's type: string slots keep the literal (`off` stays `"off"`,
R04.7), other slots understand true/false/yes/no/on/off/null, ints, floats, and YAML list/mapping
literals (an invalid literal is refused, never stored as a string nobody reads). Guards refuse
replacing a section with a scalar (unless forced) and a wrong-shaped value in a known list/mapping
slot. UPPER_SNAKE names are reported as env-shaped so P07 can route them to `.env`.

Inputs → outputs:
- `split_key(key) -> list[str]`, `join_key(parts) -> str` (escapes `\` and `.`),
  `get_path(doc, key, default=MISSING)`, `has_path`, `set_path(doc, key, value)`,
  `unset_path(doc, key) -> bool`, `resolve_path(doc, key)` (the real key tuple a path reaches),
  `coerce(raw, default_type, *, key=None)`,
  `check_write(doc, key, value, *, force=False) -> (key, value)`, `is_env_shaped(key)`.
- errors: `KeyPathError(ValueError)`, `CoercionError(ValueError)`, `GuardError(ValueError)`.

Files: `src/indus/foundation/config/keys.py`, `tests/unit/foundation/config/test_keys.py`.

Edge cases / failure modes:
- empty key, surrounding whitespace, empty segments (`a..b`, `a.`, `.a`) → `KeyPathError`.
- numeric segments index existing lists (never grow them); a scalar in the middle of a path →
  `KeyPathError` (no silent replacement).
- `coerce`: `"-5"` int, `"0.10000000000000000001"` stays a string (a float would lose digits), `nan`/`inf` stay
  strings, a quoted literal (`'"[x"'`) gives the inner string, bare `model` with a structured
  literal is parsed so the section guard can see it.
- `check_write`: bare `model` scalar over a mapping → redirected to `model.default`; any other
  section → refused without `force`; known containers (`providers` mapping, `approvals.deny` list,
  schema lists/mappings, the value already on disk) refuse the wrong shape (a mapping slot accepts
  a scalar with `force`, a list slot never).

Our design differences / decisions:
- `\\` is an escape for a literal backslash as well, so `join_key`/`split_key` round-trip any
  segment (the reference keeps backslashes verbatim, which breaks a segment ending in `\`).
- the guards raise; printing and exit codes are P07's `config set`.

Tests we will write: table tests per rule; hypothesis property `split_key(join_key(p)) == p` (runs
when `hypothesis` is installed; a seeded random version always runs).

## Decisions for the lead
- `hypothesis` (pinned for P00-11 in the pin table) is not in the dev group at d1c4371, so the
  property test uses `pytest.importorskip("hypothesis")` plus a seeded fallback that always runs.

Compare checklist (filled after the rewrite; reopened the helpers above):
- [x] escape split, greedy literal, phantom sibling (tests: `tests/unit/foundation/config/test_keys.py::test_split_key_escapes`, `::test_get_prefers_literal_dotted_key`, `::test_set_refuses_phantom_sibling`)
- [x] coercion: string slot literal, words, int, float precision, structured parse/refuse, quoted literal (test: `::test_coerce_table`)
- [x] section guard and container guard (tests: `::test_check_write_section_guard`, `::test_check_write_container_guard`)
- Deliberate differences: `\\` escape; scalar intermediates refused instead of replaced; one-item
  list leniency (`agent.disabled_toolsets`) not taken — those slots do not exist yet.
- Not taken: did-you-mean / wrong-prefix suggestions, provider route clean-up (R04.9) — P07.

## Status

Done (2026-10-18). Nothing outside the task's files.
