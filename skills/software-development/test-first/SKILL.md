---
name: test-first
description: Write a failing test first, then make it pass
version: 1.0.0
author: Indus Agent
license: MIT
metadata:
  indus:
    category: software-development
    tags: [testing, tdd, red-green-refactor]
---

# Test first

Pin the behaviour you want with a test before you write the code that provides it. The failing
test proves the test can catch the problem; the passing test proves the code solves it.

## When to use

- Adding a feature or changing behaviour in a codebase that already has tests.
- Fixing a bug: the test that reproduces it guards against its return.
- The user asks for test-driven development, or for "tests with the change".

## Procedure

1. **Find the test setup.** Look for the test folder, the runner and its config with
   `search_files` (`pytest.ini`, `pyproject.toml`, `package.json` scripts, `go test` files,
   `Cargo.toml`). Read one or two nearby tests with `read_file` and copy their style: fixtures,
   naming, assertion helpers.
2. **Write down the behaviour.** In one or two sentences: given this input, the code should do
   that. List the edge cases worth a test of their own in `todo_list`.
3. **Red.** Write the smallest test for the first behaviour with `write_file` or `patch`. Run only
   that test with `terminal` and confirm it fails *for the expected reason* (an assertion about
   the missing behaviour, not an import error or a typo).
4. **Green.** Write the simplest code that makes the test pass. Run the test again.
5. **Refactor.** With the test green, clean up names and duplication in both the code and the
   test. Run the test after every change.
6. **Repeat** for each item on the list, one behaviour per loop.
7. **Run the wider suite** before you hand the work back, so you know nothing else broke.

## Pitfalls

- A test that passes on the first run proves nothing; check that it fails without the change.
- Testing the implementation (private helpers, call counts) instead of the behaviour makes every
  refactor break the suite.
- Real clocks, network calls and random seeds make tests flaky. Inject a fake clock, stub the
  network, fix the seed.
- One giant test that checks everything fails for many reasons and explains none. Keep each test
  about one thing.
- Do not weaken an existing test to make new code pass. If the old expectation is wrong, say so
  and change it on purpose.

## Verification

- The new tests failed before the change and pass after it (you saw both runs).
- The full suite, or at least the touched package's tests, passes.
- Test names read like the behaviour they check.
