# Task: Compressor config and tokenizer (P22-11) Phase: 22 Owner: wave-21 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `trajectory_compressor.py`
(`CompressionConfig`, `from_yaml`, `_init_tokenizer`, `count_tokens`). Research 10 §3.5 and traps
R10.9, R10.10. Closed before writing.

What it does (own words): one validated settings object for `indus research compress`, loaded
from YAML with sections, every key known (an unknown key is an error, so no key is silently
inert), with a real default file shipped in the package. Plus the token counter: a Hugging Face
tokenizer pinned by repository and revision, loaded without remote code unless the user opts in
(then a warning names the repository and revision).

Shape (`CompressionConfig`, Pydantic, `extra="forbid"` everywhere):
- `tokenizer {name, revision, trust_remote_code: false}`
- `compression {target_max_tokens: 15250, summary_target_tokens: 750}`
- `protected_turns {first_system, first_human, first_gpt, first_tool: true, last_n: 4}`
- `summarization {model: null (= the profile's auxiliary model, D-16.60), base_url: null,
  api_key_secret: null, temperature: 0.3, max_retries: 3, retry_delay: 2}`
- `output {add_summary_notice: true, notice_text: <ours>, suffix: "_compressed"}`
- `processing {max_concurrent_requests: 50, per_trajectory_timeout: 300, skip_under_target: true}`
- `metrics {enabled: true, per_trajectory: true, file: "compression_metrics.json"}`

Tokenizer choice: `Qwen/Qwen3-8B` at revision `b968826d9c46dd6066d109eabc6255188de91218`
(Apache-2.0, on the licence allow-list; a plain fast tokenizer, needs no remote code). The
`research` extra pins `transformers==5.17.0` and `datasets==5.0.1` (Apache-2.0 both; uploaded
2026-09-09 and 2026-07-28, more than 14 days before exclude-newer 2026-10-04).

Inputs → outputs: `load_config(path | None)` (None → the shipped defaults); `ConfigError` on a bad
file (CLI exit 2); `Tokenizer` protocol (`encode(text) -> list[int]`); `load_tokenizer(settings,
*, loader=None, warn)` → `TokenCounter`; `TokenCounter.count(text)` (encode error → `len // 4`
and `fallbacks += 1`); `TokenizerError` on load failure or missing `transformers` (exit 2).

Edge cases: empty YAML file → defaults; a section given as a non-mapping → error; `last_n < 0`,
`target <= 0`, `summary_target <= 0`, `max_concurrent_requests < 1`, `per_trajectory_timeout <= 0`,
`max_retries < 1` → error; `--trust-remote-code` flag overrides the file.

Files: `src/indus/research/compress_config.py`, `src/indus/research/compress_defaults.yaml`,
`tests/unit/research/test_compress_config.py`, `tests/fakes/research/fake_tokenizer.py`; shared:
`pyproject.toml` (the `research` extra).

Our design differences / decisions: remote code off by default (D-16.21); summariser defaults to
the profile's auxiliary route, not a fixed vendor model (D-16.60); the key is a secret name
looked up in the profile's secret store / env view, never a raw env read; `per_trajectory_timeout`
is loadable (R10.9); the reference's `num_workers` and `save_over_limit` are dropped (they did
nothing); a default file ships in the package (R10.9); our tokenizer and notice text.

Tests we will write: defaults table; unknown key rejected (top level and inside a section); the
shipped file equals the model defaults; remote code never passed true by default (spy on a fake
`from_pretrained`) and the revision is passed; the warning when opted in (flag and file);
fallback counting on encode error; load failure → `TokenizerError`.

## Decisions for the lead
- Tokenizer `Qwen/Qwen3-8B` @ `b968826d…` (Apache-2.0).
- `summarization.api_key_secret` names a secret (store first, then the env view) used only with
  an explicit `base_url`; with neither, the auxiliary route (`auxiliary.compression` pin or the
  main route) answers.

Compare checklist (filled after the rewrite; reopened `CompressionConfig`, `from_yaml`,
`_init_tokenizer`, `count_tokens`):
- [x] defaults 15250 / 750 / protect first of each role + last 4 / temperature 0.3, 3 retries,
  delay 2 / notice on / suffix `_compressed` / 50 concurrent / 300 s / metrics on (test:
  `tests/unit/research/test_compress_config.py::test_defaults_table`)
- [x] tokenizer load failure fatal; encode error → `len // 4` (tests: `test_load_failure_is_fatal`,
  `test_encode_error_falls_back_to_length_over_four`)
- Deliberate differences: unknown keys are errors (R10.9; test `test_unknown_keys_are_rejected`);
  a real defaults file ships (`test_shipped_file_equals_the_defaults`); `per_trajectory_timeout`
  loadable; remote code off by default with a warning naming repo + revision when on (D-16.21,
  R10.10; tests `test_remote_code_is_never_passed_true_by_default`,
  `test_opting_in_warns_naming_repo_and_revision`); a revision is always passed; our tokenizer
  (Apache-2.0) instead of the reference's; summariser = the auxiliary route (D-16.60) and the key is
  a secret name, not an env var name; `num_workers`, `save_over_limit` dropped (they did nothing);
  `skip_under_target: false` now means something (under-target entries are left out of the output,
  P22-13); a missing `transformers` is a clear "install the research extra" error.
- Not taken: the base-URL → provider host table (our provider layer resolves routes).

## Status

Done (2026-10-19). Shared: `pyproject.toml` (`research` extra: `datasets==5.0.1`,
`transformers==5.17.0`). Re-locked at the landing (unchanged `exclude-newer`): 13 new packages,
all permissive (see `THIRD_PARTY_NOTICES.md`), and `fsspec` moved 2026.9.0 → 2026.6.0 because
`datasets` 5.0.1 caps it; owner checks: `regex` (… AND CNRI-Python) and the zlib/Boost code bundled
in the `pyarrow` wheels are not on the licence allow-list (LEAD-NOTES, D-16.325).
