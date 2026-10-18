# Task: Errors and ids (P01-20) Phase: 01 Owner: P01 S-core builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_ids.py` (all),
`hermes_state_keys.py` (skim: function list), `hermes_state_errors.py` (lines 1–150 skimmed for the
shape of typed errors). Closed before writing.
Evidence: none.

What it does (own words): `IndusError` carries a stable machine code, a user-facing message, an
optional hint and a detail that only goes to logs (`str(err)` is the message). Every code is listed
in `ERROR_CODES`, which later generates the protocol error table. `ids` is the only place that mints
and recognises product ids: session ids `YYYYMMDD_HHMMSS_<hex>` (6/8/12 hex), cron runs, background
ids, API session ids and 32-hex message uids, all with injectable clock and rng.

Inputs → outputs: clock + rng → id strings; any string → `IdShape | None`.

Files: `src/indus/foundation/errors.py`, `src/indus/foundation/ids.py`,
`tests/unit/foundation/test_errors_ids.py`.

Edge cases / failure modes:
- path-like (`../x`), empty, 200-char or upper-case-hex inputs → not recognised.
- session stamp with an impossible date (`20261340_…`) → not recognised.
- hex length outside 6/8/12 for sessions → `ValueError` when minting.

Our design differences / decisions:
- Subclasses carry a class-level default code, so `ConfigUnreadable("msg")` works; the base class
  takes `(code, message)` as in the row.
- Ids only recognise our own shapes (no room/run/uuid shapes until a phase mints them).
- Hypothesis is not installed in this tree (dev dep missing from `pyproject.toml`), so the
  "minted ids always recognised" property runs as a seeded 2,000-case loop.

Tests we will write: unique codes all in the table; `str` hides detail; exact ids from a fixed clock
and seeded rng; every shape recognised; garbage rejected; property loop.

## Decisions for the lead
- `hypothesis` / `pytest-asyncio` (P00-11 dev deps) are absent from `pyproject.toml`; the property
  test is a seeded loop and async tests use `asyncio.run`. Swap to hypothesis when the dev dep lands.

Compare checklist (filled after the rewrite; reopened `hermes_state_ids.py`):
- [x] stamp + hex shape, 6/8/12 widths (test:
  `tests/unit/foundation/test_errors_ids.py::test_session_id_exact`)
- [x] cron / bg / api recognisers (test: `test_every_shape_recognised`)
- Deliberate differences: ours validates the date part and anchors the whole id; theirs matches a
  prefix only.
- Not taken: room / run / uuid shapes (no minting surface yet).

## Status

Done (2026-10-18).

## Integration (lead)

- One `ERROR_CODES` table: the secret-store codes are `secrets.*` (`secrets.error`,
  `secrets.unavailable` renamed from `secret_store.*`, plus `secrets.backend_failed`,
  `secrets.unreadable`, `secrets.bad_mode` used by `secrets.py`).
- `SecretStoreUnavailable` lives here (default code `secrets.unavailable`); `secrets.py` re-exports
  it; `config/errors.py` re-exports the config classes from here.
- New tests: `test_every_explicit_code_in_src_is_listed` (AST scan: an `XError("a.b", "...")` call
  under `src/` must use a listed code) and `test_moved_classes_are_the_same_objects`.
