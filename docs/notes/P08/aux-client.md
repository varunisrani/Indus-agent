# Task: Auxiliary model client (P08-17) Phase: 08 Owner: P08 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/auxiliary_client.py` — only
the module docstring, `_resolve_auto_route` and the `call_llm` entry (the rest is vendor quirks).
Research `01` §3.21, `03` §3.12 (auto titles). Closed before writing.
Evidence: none.

What it does (own words): side tasks (compression summaries, titles, the smart approval judge,
later vision, session search, memory) ask `AuxClient.call(task, messages, …)` for one answer.
The route for a task is chosen in this order: the task's pin `auxiliary.<task>.{provider, model,
base_url, reasoning_effort, timeout_s}` (`provider: auto` or nothing = the main route and model);
then the task's own `auxiliary.<task>.fallback` list; then the main fallback chain
(`fallback_providers`). A candidate that cannot be resolved (unknown provider, no key) is skipped;
an HTTP 402 (billing) answer falls through to the next one; any other failure is the caller's.
The call is non-streaming, bounded by the task's timeout on the injected clock, and never goes
through the main loop's request path, so loop-level request hooks never see it. Usage of every
answered call is handed to a recorder with the task tag (B24). When the summariser's window is
smaller than the main model's, a warning is logged once and the smaller window is returned so the
compression trigger can be lowered (P08-02). After the first completed exchange of a session the
session service starts an auto-title task (3–7 words, our prompt) on the side; it never delays the
turn, writes with `llm` provenance (a user title is never replaced) and runs once per session.

Inputs → outputs: task name, messages, limits, config, main route → `AuxResult{text, response,
route, latency}`; usage records `(task, route, usage, latency)`; a title on the session row.

Files: `src/indus/providers/auxiliary.py`, `tests/unit/providers/test_auxiliary.py`. Shared:
`src/indus/foundation/config/schema.py` (`auxiliary.*`), `src/indus/service/session_service.py`
(title trigger after the first exchange).

Edge cases / failure modes:
- pin names an unknown provider or one without a key → skipped, next candidate;
- every candidate unusable or 402 → `AuxUnavailable` with the reasons in order;
- duplicate candidates (pin equals main) are tried once;
- timeout → `AuxTimeout` (a `ProviderError`), not a fall-through;
- model reply empty → empty text, the caller decides (titles: nothing written);
- title generation fails or the title is taken → logged at INFO/WARNING, nothing else happens.

Our design differences / decisions:
- no discovery chain over every provider with a key when no policy exists: `auto` is the main
  route, and only configured lists are walked (smaller, predictable; D-16.42 has no portal lane);
- per-task default timeouts: compression 120 s, title 30 s, approval 30 s, others 60 s;
- observers instead of plugin hooks for now (P13 attaches its `pre/post_auxiliary_call` there);
- the title prompt is ours (D-16.23).

Tests we will write:
- resolution order table (pin, auto, task fallback, main chain, duplicates, unusable skipped);
- 402 falls through, 500 does not; all unusable → `AuxUnavailable`;
- usage recorded with the task tag; timeout on a fake clock;
- small-window warning once and the lower window returned;
- title written after the first exchange through the fake aux model, the turn result returned
  before the title exists; a user title is kept.

## Decisions for the lead
- Auxiliary calls do not retry inside one candidate beyond P03's own retry (1 retry) — a side task
  should give up quickly and let the caller fall back to its deterministic path.
- `SessionService(aux=...)` is optional; with no aux client there is no auto-title.

Compare checklist (filled after the rewrite; reopened the docstring, `_resolve_auto_route`, `call_llm`):
- [x] `auto` = main provider and model first (test: `tests/unit/providers/test_auxiliary.py::test_resolution_order_table`)
- [x] Task chain before the main chain (same test)
- [x] 402 falls through (test: `tests/unit/providers/test_auxiliary.py::test_402_falls_through_to_next_candidate`)
- [x] Loop hooks not involved; usage tagged (test: `tests/unit/providers/test_auxiliary.py::test_usage_recorded_with_task_tag`)
- Deliberate differences: no discovery chain, no per-vendor client classes, no concurrency
  semaphore per task (one aux call at a time per caller is enough for now).
- Not taken: vision-specific chain, free-only switch, credit-aware output shrinking on 402.

## Status

Done (2026-10-18). Title trigger lives in the session service (shared file).

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18, lead's verify tree, on top of P07).
- Hosts: `cli/runtime.build_service` makes one `AuxClient` per run (REPL, `chat -q` and `-z` all go
  through it) and hands it to `SessionService(aux=…)`; the service puts it on
  `AgentOptions.aux`, so every agent gets it too.
- `AuxClient.bound(main, record=…)` (new): each agent binds the host's client to its own route
  (`auto` follows a `/model` switch) and to its own `UsageLedger`; observers and the once-only
  window warnings stay shared. Auto-title uses the agent's bound client.
- `window_for`: the `auto` candidate (the main route itself) answers the main window without a
  lookup (no `/models` probe for the route the session already resolved).
- Compression summaries go through `Agent.aux` (`AuxSummarizer`, which reads `AuxResult.text` and
  `response.finish_reason`); an `AuxTimeout` counts as a stall, `AuxUnavailable` as unavailable.

## Wave 47 (builder d): titles keep the whole first line

Status: done (`tests/unit/providers/test_auxiliary.py::test_clean_title`, fail-first).
Read: none.

- `clean_title` cut a model's title after seven words with no sign it was cut. It now keeps the
  whole first line up to `TITLE_MAX_CHARS` (60) and cuts a longer one at the last word boundary
  (mid-word only when that would leave under 20 characters), ending it with `…`. The storage cap
  (100, `state/titles.py`) is unchanged. The prompt still asks for three to seven words.
- The screenshot's "Chat: How do I" came from the loopback fake model (`tests/fakes/model/
  http_serve.py`), which answered title requests with `Chat:` and the first three words; it now
  answers with `Chat:` and the user's whole first line, so captures show what a real title looks
  like after `clean_title`.

Decisions for the lead:
- W47-d-2 (D-16.1042): titles are bounded by width (60 characters, word boundary, ellipsis) instead of a word
  count; the 7-word cut is retired.
