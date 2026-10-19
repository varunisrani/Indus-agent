# Task: Media tags and outbound path policy (P15-18) Phase: 15 Owner: wave-1 builder G3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/base.py`
(`validate_media_delivery_path`, `_media_delivery_denied_paths` and the deny-list constants,
recency helpers, `_normalize_media_tag_path`, the MEDIA tag patterns, masking and span deletion,
`_strip_media_tag_directives`), `gateway/media_policy.py` (all), `gateway/media_fetch.py` (all).
Research `06-gateway.md` §3.13 (media in replies), trap R06.22. LEAD-NOTES P13 wave 2 (D-16.185:
MCP servers' `MEDIA:` lines are neutralised in P13; the agent's own tags still need this policy).
Closed before writing.
Evidence: none.

What it does (own words): the agent can attach files by writing `MEDIA:<path>` (optionally quoted,
optionally with `[[audio_as_voice]]` / `[[as_document]]`) or image Markdown. The sender pulls these
out of the reply (never from code spans or fences), removes them from the text and sends native
media. Because the model (or text it was fed) writes the path, this is a security boundary: a file
is delivered only from a trusted place — the profile's gateway caches (`cache/images`, `audio`,
`video`, `documents`), operator-listed extra folders, or (unless strict mode) a file created in the
last 600 s — and only after symlinks are resolved and the resolved file passes the P05 read checks
(device files, Indus secret stores and control files, secret env files) and a deny-list of
credential and system locations (home `.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`, `.azure`,
`.config/gcloud`, `.netrc`, macOS keychains, our home's secret files, pairing data and state
database, `/etc`, `/proc`, `/sys`, `/dev`, `/boot`). Strict mode accepts allow-listed folders only.
Files in a remote terminal sandbox are fetched into the document cache only after the remote path
passes the same deny-list before and after the sandbox resolves it. Each refusal becomes a short
notice in the chat.

Inputs → outputs: `extract_media(text) -> (clean_text, [MediaRef(raw, url, kind, as_voice,
as_document)])`; `strip_media_tags(text)` (display only, for the stream); `MediaPolicy.for_scope(
scope, settings)`; `policy.check(raw) -> Verdict(path | None, reason)`; `screen_remote_path(path,
home) -> bool`; `await fetch_remote(policy, remote, raw) -> Verdict`; `await policy.resolve(refs,
remote=None) -> (accepted [MediaItem], notices [str])`.

Files: `src/indus/gateway/outbound/media_policy.py`, `tests/unit/gateway/outbound/test_media_policy.py`.

Edge cases / failure modes:
- relative paths, `~user` paths, NT namespace prefixes, embedded NUL → refused.
- missing file → "not found" notice (not a security refusal); directory or device → refused.
- symlink from a cache to `~/.ssh/id_ed25519` → resolved target checked → refused.
- a tag inside a fence or inline code → left as text, never delivered.
- glued tags (`MEDIA:/a.pngMEDIA:/b.png`) → two refs; trailing punctuation and emphasis stripped.
- recency uses birth time where the OS reports it, else mtime; a time in the future (beyond 60 s
  skew) is not "recent"; strict mode turns recency off.
- remote: strict mode never fetches; unknown sandbox home → home-relative deny entries match any
  path component; unresolvable remote path → refused; fetched copy re-checked locally.

Our design differences / decisions:
- Default mode is the reference's strict-plus-recency: no "anything not deny-listed" mode, per the
  wave's security brief (only trusted locations). Strict mode drops the recency trust.
- The deny-list applies inside safe roots too, plus the P05 `read_denial` / `blocked_device` checks.
- Paths with spaces need quotes (no on-disk probing to extend a bare path).
- Image Markdown with an http(s) URL becomes a URL ref; bare image URLs stay in the text.
- `$VAR` is not expanded in tag paths (only a leading `~`).

Tests we will write: symlink from cache to `~/.ssh/id_ed25519` refused; tag in a fence / inline
code ignored; glued tags; quoted path with spaces; recency window with fake clock (599 s ok,
601 s refused, future mtime refused); strict mode refuses a recent file and accepts an allow-listed
dir; deny-list table (`.ssh`, `.aws`, secrets.json, `.env`, `/etc/passwd`, `/proc/self/environ`);
old file in an arbitrary folder refused (a tag written by the model or arriving in tool text);
remote path screened twice (fake backend returns a symlink target into `.ssh`, fetch never called);
strict mode never fetches; notices per failure.

## Decisions for the lead
- No permissive default mode (security brief). Config keys proposed for the P15-02 section:
  `gateway.media_strict` (false), `gateway.media_allow_dirs` ([]), `gateway.media_trust_recent`
  (true), `gateway.media_trust_recent_seconds` (600). Read from a plain mapping here.
- On Linux there is no birth time: a file whose mtime the agent just bumped (e.g. `touch`) counts
  as recent outside the deny-list. Strict mode closes this; public bots should use it.

Compare checklist (filled after the rewrite; reopened `validate_media_delivery_path`, the deny-list
constants, the tag patterns and `_strip_media_tag_directives` in `gateway/platforms/base.py`,
`gateway/media_policy.py`, `gateway/media_fetch.py`):
- [x] symlinks resolved before every check (`tests/unit/gateway/outbound/test_media_policy.py::test_symlink_from_cache_to_ssh_key_is_refused`)
- [x] deny-list incl. Indus secret files and P05 read checks (`::test_deny_list_table`, `::test_system_paths_are_refused`, `::test_more_credential_stores_are_refused`)
- [x] recency window with fake clock, future times refused (`::test_recent_file_trust_window`)
- [x] strict mode (`::test_strict_mode_refuses_recent_but_accepts_allowed_dir`)
- [x] remote screened before and after resolving; strict never fetches (`::test_remote_path_is_screened_twice`, `::test_strict_mode_never_fetches`, `::test_remote_screen_without_a_known_home`)
- [x] tags outside code only; glued, quoted and emphasised tags (`::test_tag_inside_code_is_ignored`, `::test_glued_quoted_and_flagged_tags`)
- [x] model/tool-written tag for an arbitrary old file never sent (`::test_an_old_file_named_by_model_or_tool_text_is_never_sent`)
- [x] gap found: the reference denies all of `~/.config`, `~/.gcloud`, `/root`, `/var/log|lib|run`,
  backups-like stores and the browser profile → added, with our own homes exempt from a denied
  ancestor (`::test_a_profile_under_a_denied_prefix_still_sends_its_cache`).
- [x] gap found: other profiles' stores under the same root → denied by relative name
  (`::test_more_credential_stores_are_refused`).
- Deliberate differences: no permissive default (trusted places only); deny-list also inside safe
  roots; upper-case `MEDIA:` only (prose "media: /r/x" is not a tag); paths with spaces need quotes.
- Not taken: Docker container-path translation (follow-up with the P11 backend), on-disk probing
  to extend unquoted paths, JSON-string masking, kanban/board stores (no such store yet).

## Status

Done (2026-10-19). R06.22 / ACC-K26-1 rows.

Integration (lead decision, D-16.249): `gateway.media_trust_recent` defaults to **false**; the
policy reads the typed `gateway` section (`MediaPolicy.for_scope(scope)` loads it; a mapping or a
`GatewaySection` may be passed), so every default comes from the schema.
