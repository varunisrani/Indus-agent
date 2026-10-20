# Task: Protocol conformance and drift checks (P19-18) Phase: 19 Owner: P19 integrator B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `website/docs/user-guide/features/acp.md`
(client setup only: how Zed, VS Code and JetBrains launch an ACP agent and what they show). Vendor
material read (third party, not the reference): the ACP specification repository
`agentclientprotocol/agent-client-protocol` at tag `schema-v1.19.0` (commit `a213df5`),
`schema/v1/{schema,schema.unstable,meta,meta.unstable}.json` and its `LICENSE`; the installed SDK's
`acp/meta.py` and `acp/schema.py` header (both say "Schema ref: refs/tags/schema-v1.19.0"). Our
`src/indus/acp/*`, `src/indus/mcp_serve/server.py`, the P19 notes, `tests/contract/acp/harness.py`,
`tests/fakes/acp_client.py`, `tests/contract/mcp_serve/conftest.py`. Reference closed before writing.
Evidence: none.

What it does (own words): three guards that fail when our protocol surfaces drift from what the
vendors define or from what we promised clients.

- **ACP schema drift.** A verbatim snapshot of the vendor schema for the pinned SDK (stable and
  unstable halves plus their method tables) lives under `tests/fixtures/acp/`. The test first
  checks that the installed SDK was generated from the same schema tag and protocol version as the
  snapshot (a new SDK pin needs a new snapshot). It then collects every vendor name our adapter
  uses — from the code itself, not from a hand list: the handler table (agent methods), the
  methods we call or notify on the editor, the `sessionUpdate` kinds, tool kinds, permission
  option kinds, tool call statuses and stop reasons written in `src/indus/acp`, the capability
  fields our `initialize` answer sets and the client capability we read — and checks each exists
  in the snapshot in the tier we expect (stable unless listed as unstable). A checker fed a copy of
  the snapshot with one kind removed must report exactly that kind.
- **ACP transcript replay.** `tests/fixtures/acp/zed_session.jsonl` is an editor session we wrote
  ourselves in the shape Zed sends it: `initialize` → `session/new` → a prompt whose tool asks
  for approval (the editor picks "Allow once") → `session/load` → `session/list` → a second prompt
  whose card stays open and is ended by `session/cancel`. The test drives the adapter through it
  on the local host and on the attached host (the P17 daemon endpoint harness), normalises the
  frames it receives (ids, times, paths, versions become placeholders), and compares both to one
  golden file. Every `session/update` and every response in the golden also validates against the
  snapshot's JSON schema.
- **MCP tool list.** The `tools/list` answer of `indus mcp serve` (names, descriptions, input
  schemas) is compared with a snapshot file, without the approval queue (8 tools) and with it
  (10 tools).

Inputs → outputs: snapshot files + our code → a list of drift findings (empty = pass); transcript
→ normalised frames == golden; MCP server → tool list == snapshot.

Files: `tests/contract/acp/test_schema_drift.py`, `tests/contract/acp/test_transcript_replay.py`,
`tests/contract/mcp_serve/test_tool_list.py`, `tests/fixtures/acp/schema-v1.19.0.json` (+
`.unstable.json`, `meta-v1.19.0.json`, `meta-v1.19.0.unstable.json`, `LICENSE-schema`),
`tests/fixtures/acp/zed_session.jsonl`, `tests/fixtures/acp/zed_session.golden.jsonl`,
`tests/fixtures/mcp_serve/tool_list.json`; THIRD_PARTY_NOTICES (snapshot licence),
traps/acceptance rows.

Edge cases / failure modes:
- Schema tier: `session/fork`, `elicitation/create`, `sessionCapabilities.fork` and
  `clientCapabilities.elicitation` exist only in the unstable half at v1.19.0; `session/list` and
  `session/resume` are stable there (the SDK 0.12.1 router still flags resume as unstable — we do
  not use its router, our dispatcher serves every method).
- Ordering in the transcript: post-`session/new` updates come after the response (R09.9); load's
  history comes before its response; frame order between independent notices must be the same on
  both hosts or the golden would flap — any difference found is a host bug, not a normalisation.
- A vendor rename in a future pin: the drift test names the missing names; the fix is a new pin
  and snapshot, never an edit of the snapshot (phase rule).
- `live` lane (Zed headless): no Zed binary on CI or this machine; the test is marked `live` and
  skips with a reason when `zed` is not on PATH.

Our design differences / decisions:
- The reference has no conformance tests of this kind; everything here is ours.
- Snapshot kept as four vendor files (stable + unstable schema and method tables) so tiers can be
  checked; licence Apache-2.0 (the spec repository's LICENSE at the tag), recorded in
  THIRD_PARTY_NOTICES and copied beside the files.
- No regeneration switch: on a difference the replay and tool-list tests write what they got to
  their temp folder and name it; the golden is replaced by hand for an intended change, with a
  note saying why (the transcript itself is never edited to make a test pass).
- The replay waits for the agent's post-answer notices before the editor's next step (as a
  recorded session would show them), so frame order does not depend on load.
- The MCP snapshot keeps one compact line per tool (our layout; a pretty-printed pydantic schema
  dump matched generated vendor JSON elsewhere line for line).

Tests we will write:
- drift: SDK ref = snapshot ref; every used name present with its tier; mutated snapshot reports the
  removed kind; used-name collection finds what the adapter really sends (sanity floor).
- replay: local == golden, attached == golden; frames validate against the snapshot schema.
- MCP: tool list without and with the approval queue == snapshot; every input schema is an object.

## Decisions for the lead
- Expected tiers live in the drift test (`UNSTABLE`), not in product code: our dispatcher does not
  gate unstable methods (no SDK router), so nothing in the adapter reads a tier.
- Golden for the replay is a separate fixture `zed_session.golden.jsonl` (the phase lists only the
  transcript).

Compare checklist (filled after the rewrite; reopened `website/docs/user-guide/features/acp.md`):
- [x] Editors the reference documents (Zed `agent_servers` custom command + args, the VS Code ACP
  client's `acp.agents`, JetBrains plugins) all launch one stdio agent; the transcript drives that
  same adapter (`AcpAgent.dispatch` over the SDK connection) in the order Zed sends frames (test:
  `tests/contract/acp/test_transcript_replay.py::test_the_transcript_gives_the_golden_frames_on_both_hosts`).
- [x] Approval options on the wire, including the session tier carried by option id
  (`allow_session`, kind `allow_always`), are pinned in the golden's permission requests and
  validated against the vendor `RequestPermissionRequest` (test:
  `tests/contract/acp/test_transcript_replay.py::test_every_frame_of_the_session_matches_the_vendor_schema`).
- [x] Gap found while comparing the hosts (not in the reference): the attached host answered the
  model picker without the provider and, with no resolvable profile route, without a picker at
  all — fixed by a route seam on `AttachedHost` and the provider of a resumed session (D-16.637),
  pinned by the golden on both hosts.
- [x] Gap found under load: a permission card could reach the editor before the `tool_call` it is
  about — fixed by flushing the projection before every card (D-16.637), pinned by the golden
  (25 runs with three busy cores, no difference).
- Deliberate differences: the reference documents three approval options and "timeout or error
  denies"; ours offers five mapped by id and reports a timeout as its own decision (P19-11), as the
  golden shows.
- Not taken: a Zed headless run (no editor binary or headless driver here; the `live` test reports
  the skip).

## Status

Done (2026-10-20), P19 integrator B. Built beside the P19-08..17 landing; changed outside the
task's test files: `src/indus/acp/permissions.py` (flush before a card), `src/indus/acp/attached_host.py`
(route seam, resumed provider), `tests/contract/acp/harness.py` (the attached harness resolves the
daemon's route), THIRD_PARTY_NOTICES (snapshot licence), traps/acceptance rows. The attached
transcript runs against the in-loop P17 daemon host and endpoint, whose `session.mutate` is the
real one since D1 (D-16.602 built, D-16.638); the golden did not change when D1 landed. The
separate-process path (`indus gateway run` + `indus acp`) is covered by
`tests/e2e/acp/test_acp_daemon.py` (prompt, model picker, a daemon-answered command, fork).
