# Task: Acceptance map + traps ledger with checks (P06-15, D-15.3) Phase: 06 Owner: lead

Read: none in the reference (our tooling); `docs/plan/03-feature-inventory.md`, research §6
lists, `tooling/checks/{acceptance,traps,ledger}.py` (since P00-04).

What D-15.3 needed beyond P00-04: the parsing, the missing-id, missing-test and `--strict` rules
were already there (`tests/test_ledgers.py`). Added:
- skipped tests do not count: a cited test with `@pytest.mark.skip` (on it or its class),
  `pytest.skip(...)` as its first statement (after a docstring), or a module whose `pytestmark`
  holds `pytest.mark.skip` fails both checks; `skipif` / `importorskip` are conditional and still
  count (`tooling/checks/ledger.py`);
- `tooling/checks/tests/test_acceptance.py` and `test_traps.py`: seeded violations (P0 feature
  without an id, each skip form, a skipped module, a row with neither test nor review, a new R-id
  in a research spec without a row, a skipped pinning test) fail; the repository pages pass
  non-strict; `--strict` fails on review rows.

Pages: rows landed by P06 (slices A, B, C) were merged; the integration added the Demo 1 e2e,
launch-adapter, slash-bridge, `/model`-across-resume and `tool.started` redaction tests to ACC-B2-1,
B23-1, I4-1, I5-1, I7-1, I9-1, I11-1, I26-1, I55-1, A53-2 and traps R01.3, R01.4, R04.1, R04.27,
R05.4, R05.22. Rows of later phases keep their `Review: … (<task id>)` lines.

`python -m tooling.checks acceptance traps`: both ok (non-strict).
