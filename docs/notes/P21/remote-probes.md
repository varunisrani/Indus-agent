# Task: Registration and requirement probes, the rest (P21-12) Phase: 21 Owner: P21 slice B3 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/terminal_tool_backends.py`
(`_create_environment`, `_check_requirements`, `_modal_pre`, `_daytona_post`,
`terminal_backend_unavailable_reason`), and for the grace rule `tools/registry.py` (the check-fn
cache). Closed before writing. Evidence: none.

Already landed (B1+B2, lead wiring): the four rows are in `default_entries`, each with a pure probe
(`singularity_probe`: CLI on PATH; `modal_probe` / `daytona_probe` / `vercel_probe`: the extra's
distribution in package metadata, then the backend's `requirement_reason`). Not built: the flapping
guard (R02.13) and the unscoped-probe rule (R02.29). That is this slice.

What it does (own words): a probe answer can wobble for reasons that have nothing to do with the
backend: a secret store that times out once makes a token look missing, a PATH lookup on a busy
network home can fail. If the terminal tool disappears on one such wobble the model tells the user
"there is no terminal". So each of the four rows' probes is wrapped in a guard, kept per profile:

- success → remembered as last-good (time stamp), any failure streak is forgotten;
- a failure within `FLAP_GRACE_S` (120 s) of the last success serves the last-good answer (None) and
  starts a failure streak; a warning is logged;
- a further failure while the streak is younger than `CONFIRM_GAP_S` (30 s) still serves last-good;
- a failure when the streak is at least 30 s old (two consecutive failures 30 s apart), or with no
  success in the last 120 s, is honoured: the reason is returned and last-good is dropped, so the
  next failure is honoured at once and only a new success brings the backend back;
- a check that raises answers "the <backend> check failed: <type>: <text>", counted as a failure.

Unscoped probes (R02.29): a probe asked with no scope (`None`, e.g. a startup notice in a host that
has no profile yet) or whose check raises `ScopeUnresolved` logs at debug level, records nothing and
returns a fixed "not checked yet" reason. Nothing is cached, so the first call with a scope runs the
check for real (logged once at info as a re-probe).

Inputs → outputs:
- `GuardedProbe(name, check, *, clock=None, grace_s=FLAP_GRACE_S, confirm_s=CONFIRM_GAP_S)`;
  `__call__(scope | None) -> str | None` (the row's probe); `last_reason(scope) -> str | None`
  (what the last honoured or served-through failure said, for `doctor` and the startup notice);
  `clear()`.
- `guard_entry(entry) -> BackendEntry` (the same row with its probe wrapped; a row without a probe
  is returned unchanged); `GUARDED_BACKENDS` = the four names; `guarded_probe(name)` returns the
  process-wide guard of a row (one per name, so doctor and the registry see the same history).
- `default_entries` (shared, `registry.py`) wraps the four rows through `guard_entry`.

Edge cases:
- Two profiles never share a history (keyed by profile home, R02.8).
- The clock is the scope's clock unless one is injected (tests use a fake clock).
- A missing extra is a failure like any other; it never flaps in practice, and it names the extra.

Our design differences / decisions:
- The guard is per backend row and sits under the tool registry's own probe cache (which keeps its
  60 s grace, P04). Both rules agree on "one flake does not remove the tool"; ours adds the 30 s
  confirmation the phase asks for. Decision for the lead: the two layers are kept separate (the tool
  registry's grace covers every tool; this one only the cloud rows).
- "Two consecutive failures 30 s apart" read as: a failure streak whose first and latest failure
  are at least 30 s apart; failures closer together are still served last-good while inside the
  120 s window.
- Docker keeps its own `Availability` (60 s grace, 30 s cache); not changed here.

Tests planned (`tests/unit/backends/test_remote_probes.py`):
- probe table per backend (installed/missing extra, credentials present/absent, CLI found or not);
- grace on a fake clock: ok → fail (served) → fail 10 s later (served) → fail 30 s after the first
  (honoured) → fail again (honoured) → ok (available);
- failure more than 120 s after the last success is honoured at once;
- a raising check is a failure with the exception named;
- unscoped (None and `ScopeUnresolved`) answers "not checked yet", caches nothing, next scoped call
  runs the check;
- profiles are independent;
- `default_entries` rows for the four backends carry guarded probes; seven built-ins listed.

Compare checklist (after writing):
- [x] reference keeps one module-level "last reason" for doctor/startup; ours keeps it per profile
  per row (`last_reason`).
- [x] reference grace is one 60 s window on the generic check cache; ours adds the 30 s confirmation
  on the four rows (phase rule), and serves last-good without caching.
- [x] no network, no SDK import in any probe (unchanged from B1/B2).
