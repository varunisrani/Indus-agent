# Task: Bedrock Converse transport (P08-10) Phase: 08 Owner: P08 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/transports/bedrock.py`
(all), `agent/bedrock_adapter.py` (skim: message/tool conversion, stream event handling, region
and credential detection, streaming-denied check, context table). Closed before writing.
Vendor facts checked against AWS documentation (read 2026-10-18):
- Converse / ConverseStream API reference
  (https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html,
  `API_runtime_ConverseStream.html`): `POST /model/{modelId}/converse` and `/converse-stream` on
  `bedrock-runtime.{region}.amazonaws.com`; body `messages`, `system`, `inferenceConfig
  {maxTokens, temperature}`, `toolConfig {tools: [{toolSpec {name, description, inputSchema
  {json}}}], toolChoice}`, `additionalModelRequestFields`; content blocks `text`, `image {format,
  source {bytes}}`, `toolUse {toolUseId, name, input}`, `toolResult {toolUseId, content, status}`,
  `reasoningContent {reasoningText {text, signature} | redactedContent}`; stop reasons `end_turn`,
  `tool_use`, `max_tokens`, `stop_sequence`, `guardrail_intervened`, `content_filtered`; usage
  `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens`; stream events
  `messageStart`, `contentBlockStart`, `contentBlockDelta`, `contentBlockStop`, `messageStop`,
  `metadata`, and exception events.
- Event stream encoding (https://docs.aws.amazon.com/AmazonS3/latest/API/RESTSelectObjectAppendix.html
  and the Transcribe "event stream encoding" page): prelude (total length, headers length, prelude
  CRC32), headers (name length byte, name, type byte, value; type 7 = string with a 2-byte
  length), payload, message CRC32; headers `:message-type` (`event` / `exception`),
  `:event-type`, `:exception-type`, `:content-type`.
- Signature Version 4 (https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html
  and the published SigV4 test suite): canonical request, string to sign
  `AWS4-HMAC-SHA256`, signing key chain `AWS4<secret>` → date → region → service → `aws4_request`.
- Bedrock API keys (bearer token, `AWS_BEARER_TOKEN_BEDROCK`) as an alternative to SigV4.
Evidence: none.

What it does (own words): speaks the Bedrock Converse wire for the `bedrock` API mode with our
own HTTP (httpx) and our own SigV4 signer. History → Converse messages (system blocks separate;
tool rows become `toolResult` blocks in a user turn; same-role neighbours merged; a blank text
becomes a fixed placeholder because Converse refuses blank text; the conversation starts with a
user turn). Streaming answers come back as AWS event-stream binary frames, decoded incrementally and
turned into our events. Claude models get extended thinking through
`additionalModelRequestFields`. A context-length table covers common Bedrock model ids.

Inputs → outputs: route (base URL `https://bedrock-runtime.<region>.amazonaws.com`, model id) +
`ChatRequest` → signed `httpx.Request`; event-stream bytes → events; JSON → response.

Credentials: a route key (Bedrock API key) → `Authorization: Bearer`; else the standard chain read
from the scope: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (+ `AWS_SESSION_TOKEN`), then the
shared credentials file (`AWS_SHARED_CREDENTIALS_FILE` or `~/.aws/credentials`, profile from
`AWS_PROFILE`, default `default`). Region: the base URL's host, else `AWS_REGION`,
`AWS_DEFAULT_REGION`, else `us-east-1`. Nothing found → `BedrockCredentialsMissing` naming the
variables to set.

Files: `src/indus/providers/transports/bedrock.py`, `src/indus/providers/aws_sigv4.py`,
`src/indus/providers/eventstream.py`, `tests/unit/providers/transports/test_bedrock.py`,
`tests/fixtures/captures/bedrock/{text,tool_call}.json`, `tests/fakes/model/wire_bedrock.py`;
shared: registry entry and the stream-framing hook in `providers/transports/__init__.py`, one line
in `providers/streaming.py` (use the transport's framing), `providers/classify.py` (an IAM denial of
`InvokeModelWithResponseStream` is `stream_unsupported`, so the P03 switch turns streaming off for
the session), `providers/context_length.py` (Bedrock table before the family table).

Edge cases / failure modes:
- a frame with a bad prelude or message CRC → `ProviderStreamError` (`bad_frame`); a frame split
  across network chunks is reassembled; a frame larger than 16 MiB is refused.
- `:message-type: exception` frames → `ProviderStreamError` with the exception type as code
  (`throttlingException` → rate limit wording, `validationException`, ...).
- stream ends without `messageStop` → `incomplete_stream`.
- model ids with `:` (`...-v1:0`) and ARNs are percent-encoded in the path; the signer encodes the
  path once more (SigV4 rule for every service but S3).
- `guardrail_intervened` / `content_filtered` → `content_filter`.
- tool input that is not a JSON object → `{"_raw": text}`.
- `AWS_BEARER_TOKEN_BEDROCK` is already kept out of child processes by P05.
- a route key read from `OPENAI_API_KEY` (the custom-provider default) is never sent to AWS; the
  transport signs with AWS credentials instead.

Our design differences / decisions:
- No botocore: the pin table has no SDK row, so (per the lead) raw httpx + a small stdlib signer.
  "Missing extra → named error" became "missing credentials → named error". No SSO, assume-role or
  instance-metadata credentials (use env vars or the shared file; a `bedrock` extra with botocore can
  add them later).
- Reasoning only for Claude model ids (thinking budget per the P03 Anthropic ladder); other models
  ignore the effort setting.
- No cache points, no guardrail config, no probing of the window by provoking errors.

Tests we will write: SigV4 published test vectors (get-vanilla, post-x-www-form-urlencoded, signing
key example); event-stream decoder on the capture split at every byte; CRC failure; exception
frame; tool-use round trip (history → Converse → stream → tool call → next request); stop-reason
table; missing credentials → named error; shared-file credentials; bearer token; IAM streaming
denial → non-streaming retry through the P03 driver.

## Decisions for the lead
- Captures use the landed P03 JSON format (base64 chunks hold the binary frames) instead of `*.bin`.
- No `bedrock` extra was added to `pyproject.toml` (nothing to install).

Compare checklist (filled after the rewrite; reopened the two reference files):
- [x] blank text → placeholder, user-first alternation (test: `tests/unit/providers/transports/test_bedrock.py::test_conversion_golden`)
- [x] stream events keyed by block index, tool input assembled (test: `::test_stream_tool_use_round_trip`)
- [x] streaming denial → non-streaming (test: `::test_stream_denied_switches_to_non_streaming`)
- Deliberate differences: the reference calls boto3; we sign ourselves. The reference adds cache
  points and redacted-reasoning recovery; not taken.

## Status

Done (2026-10-18). Captures synthetic.

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18). No bundled `bedrock` profile yet: it needs a
region-specific base URL and the AWS credential chain instead of one key variable, which the
bundled-profile rules (vendor = one key on one host) do not express — recorded for P08 wave 2
(D-16.42 lists Bedrock). The `OPENAI_API_KEY` refusal is D-16.103. `inputTokens` = uncached input
agrees with P08-08's ledger; confirm with the first live capture.
