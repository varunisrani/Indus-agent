# P08 wave 2 integration items (lead)
- Order: a, b. Both edit core/agent.py BoundCaller.call near caller_for; schema.py; turn_state.py.
- B: delete tests/integration/core/_stubs_b.py; drive the soak's 429 storm through A's real pool + fallback_providers (assertions on keys/hosts should hold).
- B FAST TIER DECLARATIONS: the bundled fast_body/fast_headers/fast_models for anthropic (speed: fast, beta header fast-mode-2026-02-01, models claude-opus-5/claude-opus-4-8) and openai (service_tier: priority; gpt-5, gpt-4.1, o3, o4-mini) were NOT verified against vendor docs. Verify against the vendors' public API docs (WebFetch); keep only what the docs state, with source+date in the note; otherwise remove the bundled declaration (fast mode then needs explicit profile config) and record.
- B: record follow-ups — streamed responses carry no served service_tier/usage.speed; approval observer hooks around the judge (P13); two-hour live session (owner item).
- B decisions accepted: judge as plain async callable (safety may not import providers); /fast state per process; breaker per ApprovalState.
- A: record follow-ups — invalid_tool strikes still end the turn (not chain); subagent inheritance untestable until P10; add `model_entitlement` reason to the classifier (A uses ENTITLEMENT_PHRASES).
- A intended differences → D-ids (sticky per-session selection; same-key retry inside P03 retry loop; eager fallback only on upstream_rate_limit; no short cap for a sole credential's cooldown).
- A+B merge in core/agent.py BoundCaller.call: A routes through PoolRuntime.call after finish_request; B layers the fast tier right after finish_request — order: finish_request → fast tier → pool/route call.
- Perf: re-run the two load-sensitive perf tests 6x alone vs main.
