# Task: Feishu document tools and comment intake (P16-15) Phase: 16 Owner: wave-25 builder E9

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/feishu/feishu_comment.py`
(skim: event parsing, filters, comment API calls, reply delivery and chunking, prompt instructions),
`feishu/feishu_comment_rules.py` (skim: rule and pairing files), `tools/feishu_doc_tool.py`,
`tools/feishu_drive_tool.py`, `tools/feishu_lark.py` (tool schemas, request shapes, registration),
`toolsets.py` (the feishu rows only). Research 02 §3.5, §3.28; research 06 §3.3. Evidence: none.
Interop: Feishu docx `GET /open-apis/docx/v1/documents/{document_id}/raw_content`; Drive v1
comments `GET /open-apis/drive/v1/files/{file_token}/comments` (`file_type`, `is_whole`,
`page_size`, `page_token`, `user_id_type`), `GET .../comments/{comment_id}`,
`GET .../comments/{comment_id}/replies`, `POST .../comments/{comment_id}/replies` (body
`content.elements[].text_run.text`), `POST .../comments` (body `reply_list.replies[].content`);
event `drive.notice.comment_add_v1` (`comment_id`, `reply_id`, `is_mentioned`, `notice_meta`
with `file_token`, `file_type`, `notice_type`, `from_user_id`, `to_user_id`).

What it does (own words): two pieces built on the P16-14 adapter's credentials.

1. **Tools** (C40), in the Feishu bundle only: `feishu_doc_read` (a document's plain text by token
   or by a Feishu/Lark document link — the link is only parsed for its token, never fetched) and
   four comment tools: `feishu_drive_list_comments`, `feishu_drive_list_comment_replies`,
   `feishu_drive_reply_comment`, `feishu_drive_add_comment`. They call the Open API with the
   profile's `FEISHU_APP_ID` / `FEISHU_APP_SECRET` (the same values the adapter uses), are
   available only where those exist, and return JSON text. What a document or a comment says is
   written by other people, so every result is wrapped in the untrusted-data fence before the
   model sees it, capped at the source (2 MiB read, 40,000 characters registered result cap) and
   spilled like any other tool result past the budget. The two write tools post text only (plain,
   escaped), are marked mutating, and never take a URL.
2. **Comment intake**: a `drive.notice.comment_add_v1` event (over either transport) that mentions
   the bot, is addressed to it, is not its own, and is a new comment or reply becomes one
   `InboundEvent` keyed per document thread: chat id `cmt_<file_type>_<file_token>`, thread id =
   the comment id (`whole` for whole-document comments), chat type `thread`, user = the commenter's
   open id. The text is the comment reply's text (the bot's own mention removed) with a short
   context line naming the document token and the quoted passage (quoted text cut at 500
   characters). Replies the agent sends to such a chat go back as comment replies (plain text,
   pieces of 4,000 characters); a local comment that refuses replies (code 1069302) and
   whole-document threads get a new whole-document comment instead. Repeats (same reply id) are
   dropped.

Inputs → outputs: tool arguments → JSON text results; comment notices → `InboundEvent`s; sends to
`cmt_…` chats → comment replies.

Files: `src/indus/gateway/adapters/feishu/tools.py`, `src/indus/gateway/adapters/feishu/comments.py`,
`tests/unit/gateway/adapters/feishu/test_tools.py`; uses the P16-14 client and wire fake.

Edge cases / failure modes:
- tools: no credentials → tool unavailable (probe false); missing or malformed token → error text
  without a call; API error codes → bounded error text (no secrets); a document over 2 MiB → cut
  with a note; links to other hosts → refused as a token, never fetched; page size clamped 1–100.
- intake: not mentioned, not addressed to us, our own comment, other notice types, missing ids →
  no event; the comment text cannot be fetched → the event still goes out with a placeholder; one
  reply id → one event even when the notice is delivered twice.

Our design differences / decisions:
- Comment events go through the normal gateway pipeline (authorization, pairing, session per
  document thread, the agent's normal tools and replies) instead of a side agent run with its own
  history file, rule file and pairing file; the core's allowlists decide who may use it.
- No document-meta, wiki reverse lookup or referenced-document expansion: links in comments are
  never followed (the agent can call `feishu_doc_read` with a token if it wants more).
- Add-comment uses the documented create-comment route (`POST /files/{token}/comments`).
- Tool names spelled as the plan's glossary (`feishu_doc_read`, `feishu_drive_*`).

Tests we will write:
- every Feishu tool absent from `indus-cli`, `indus-telegram`, `indus-cron`, the webhook floor and
  `core`; present in `indus-feishu` (with the proposed toolset rows) and nowhere else;
- `feishu_doc_read` output fenced and budgeted (a long document spills / is cut by the registered
  cap; a 3 MiB document is cut at the source);
- read by link token, link to another host refused, comment tools' request shapes over the wire
  fake, probe false without credentials, errors without secrets;
- a mention comment creates exactly one event (and a duplicate notice none), non-mention / self /
  wrong addressee none; reply to the comment chat posts a comment reply; 1069302 falls back to a
  whole comment.

## Decisions for the lead
- **Core change needed (not made):** the bundle rows in `tools/toolsets.py` (P04/P15 file) — a
  platform-gated named set `"feishu"` (`platforms=frozenset({"feishu"})`, filled from the registry)
  and `"indus-feishu": Toolset("Feishu / Lark chats", includes=(CORE, "feishu"))`, plus
  `"indus-feishu"` in `indus-gateway`'s includes. Until then `default_toolsets("feishu")` finds no
  bundle and a session that selects every toolset would see the registry-only `feishu` set (R02.25).
- **Wiring needed (not made):** someone has to call `feishu.tools.register_tools(registry)` on the
  registry gateway sessions use (`daemon/process.py` after `load_builtins`, or a
  `BuiltinModule` row — the second makes `indus.tools` name a gateway module by string).
- Optional: add `"feishu_"` to `core/untrusted.UNTRUSTED_PREFIXES`; the tools fence their own
  output today, and fencing twice is harmless.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] the five tool names and their request shapes: raw content, comment list (`file_type`
  default docx, `user_id_type=open_id`, `is_whole`, `page_size`, `page_token`), replies, reply
  body `content.elements[text_run]`, text escaped (`&`, `<`, `>`) (test:
  `test_tools.py::test_comment_tools_speak_the_drive_api`)
- [x] tools only in the Feishu bundle (Verify "Tool absent from indus-cli bundle":
  `::test_the_tools_are_absent_from_every_other_bundle`)
- [x] doc read output budgeted (Verify: `::test_doc_read_is_fenced_and_budgeted`); links never
  opened (`::test_links_are_read_for_their_token_and_never_opened`); availability by credentials
  (`::test_the_probe_needs_the_apps_credentials`)
- [x] comment notice filters: self-authored, not addressed to the bot, other notice types,
  missing fields, not mentioned (`::test_comment_notices_that_are_not_for_us_make_no_event`)
- [x] one event per mention (Verify "comment mention creates one event":
  `::test_a_comment_mention_creates_one_event_keyed_per_document_thread`), whole-document comments
  in their own thread
- [x] reply delivery: pieces of 4,000, local reply, fallback to a whole comment on 1069302, whole
  threads as new comments (`::test_replies_to_a_comment_chat_go_back_as_comments`)
- Deliberate differences: comments run through the normal gateway pipeline and session (the
  reference runs a side agent with its own history, rule file and pairing file); the add-comment
  tool uses the documented create-comment route (the reference posts to a `new_comments` route);
  tool results are fenced as untrusted data and windowed (the reference returns the whole text);
  the tools are offered only where the app credentials exist (the reference checks the SDK).
- Not taken: the OK reaction on the comment while the agent works, document meta (title / URL)
  lookups, wiki reverse lookups and referenced-document expansion (links are never followed), the
  `NO_REPLY` sentinel (the core's own silence handling applies), the timeline prompt with its
  instructions (the agent sees the comment text and a context line).

## Status

Partly done (2026-10-20): tools, intake and tests are done; the bundle rows in `tools/toolsets.py`
and the `register_tools` call are core changes left to the lead (see "Decisions for the lead";
the membership test uses the proposed rows until they land).
