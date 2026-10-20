# Integrate parked P16 adapters: E6 (P16-11 Microsoft Teams, P16-12 Graph change-notification receiver) + E7 (P16-13 Google Chat, P16-20 QQ Bot)

Worktree: /tmp/claude-0/-home-user/3c64a974-489c-5625-af4d-c59c9cfe7428/scratchpad/verify10 (from main c3403ea). Do NOT
commit. The lead commits and lands. Never write under the real ~ or /root. No network.
Running in parallel (do not touch their areas): P17 wave-2 integrator in verify9 (service/daemon_host, viewers,
creation_policy, ws_auth, service/rpc, gateway/daemon/attach_verbs, side_services, control_socket), builders E8
(adapters dingtalk, wecom), E9 (feishu), E10 (bluebubbles, simplex, buzz).

Patches (scratchpad/patches/, base d50d5d3): w25-e6.patch then w25-e7.patch. Main has since landed P16-04/10/23/26/27
(registry rows, PlatformEntry.lock_env, BUILTIN_PUBLIC_ENV, mask_number in _identity.py). Read .agents/wip/w25/README.md
(E6 + E7 sections, INCLUDING the LEAD RULINGS) in full, the four builders' notes in docs/notes/P16/, LEAD-NOTES
"P16 E4 + E5 landing notes (integrator)" (the precedent) and "P16 E2 + E3 landing notes", docs/plan/phases/
P16-more-platforms.md rows 11, 12, 13, 20, AGENTS.md, HANDOFF.md.

Steps:
1. `git apply --3way` E6 then E7; resolve as unions (contract conftest one import per case; config schema rows;
   pyproject extras google-chat, qqbot; THIRD_PARTY_NOTICES rows; traps rows).
2. LEAD RULINGS (from the README), all with tests:
   a. teams/botauth.py: replace the hand-rolled RS256 verification with `cryptography` RSA PKCS#1 v1.5 + SHA-256 verify
      (base dependency). Keep the claim checks. No custom crypto maths in src/ (grep for pow(..., e, n)-style code).
      Consider sharing one RS256/JWKS helper between Teams and Google Chat if it stays small (optional).
   b. Graph replies with no forward configured must not be reported delivered: typed non-delivered result + one log.
   c. The PEM-shaped literal in an E7 test: build the armour at runtime or generate a throwaway key.
3. Registry rows + BUILTIN_NAMES + BUILTIN_CREDENTIALS exactly as in the README (teams, msgraph, google_chat, qqbot);
   msgraph in send_command._NOT_TARGETS; decide lock_env for each (precedent: lock on the stable account id, not a
   rotating token/secret — e.g. teams: TEAMS_CLIENT_ID + TEAMS_TENANT_ID; qqbot: QQ_APP_ID; google_chat: the service
   account email/client id if available, else the credential) and record it.
4. Daemon wiring where the seams exist: Graph `GraphReceiver.set_forward(...)` through the router with
   `platforms.msgraph.deliver_to`; Google Chat drain probe — prefer adding a small base Adapter seam
   (`set_drain_probe`) the daemon calls on platform up for every adapter that wants it, else wire the adapter-specific
   call; record what remains.
5. `indus send` tests through the real send_command.build_adapter for teams, google_chat, qqbot (send-only).
6. Re-lock (`uv lock`): only extras metadata should change (cryptography, httpx-ws already locked). Pin rows in
   00-layout §4 if the repo records extras pins there.
7. Acceptance rows: ACC-K12-3 (Teams), ACC-K12-4 (Graph webhooks), ACC-K12-6 (Google Chat), and the QQ Bot row the
   inventory implies; test refs.
8. Security review: JWT checks (alg allow-list, iss/aud/exp/nbf, kid cache + rate-limited refresh, key size, never
   alg none/HMAC/unsigned, never fetch header URLs); Teams service-URL allow-list; Graph validationToken echo capped,
   clientState constant-time, notifications untrusted and never commands; Google Chat exact media host list and
   Google-only token_uri; QQ tokens never logged; credentials masked in logs/reprs/errors; httpx request-line
   identifiers (see the E4+E5 follow-up) not made worse.
9. D-ids: D-16.440 … D-16.459 in docs/plan/16-what-we-change.md §2.7 (+ §3.1 range row). LEAD-NOTES section
   "P16 E6 + E7 landing notes (integrator)".
10. No `# STAND-IN` first line anywhere; no conflict markers (grep '^<<<<<<< ' and '^>>>>>>> ').
11. Full gate, all green, no extras installed (`uv sync --locked`):
    uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run lint-imports
    INDUS_STRESS_SECONDS=10 uv run pytest -q -p no:cacheprovider -rf -m "not perf"
    uv run pytest -q -p no:cacheprovider -m perf     # alone, after (other worktrees may be running; if a perf bound
                                                     # fails, re-run it alone and report both)
    uv run python -m tooling.checks --require-ref
    CI=true pnpm test --maxWorkers=1   (run `pnpm install --frozen-lockfile --offline` first if node_modules is missing)
    A failure that passes alone must pass alone 3x AND be root-caused; never skip or mark tests.
12. Credential-literal scan of the diff (incl. PEM headers, JWT-shaped eyJ…eyJ, Google AIza keys), no stray processes,
    nothing under /root or ~.
13. Usage / restarts: the lead may message you to wrap up; if the container restarts, re-check the tree and re-run
    killed steps. Stage the final tree with `git add -A`.
Report: files changed beyond the patches, rulings applied, registry rows + lock keys, wiring done/owed, D-ids, security
findings/fixes, gate counts, follow-ups.
