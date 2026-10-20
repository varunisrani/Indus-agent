# Task: Prompt content conversion (P19-08) Phase: 19 Owner: wave-30 builder A2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/content.py` (block →
user content, resource links, embedded resources, the 512 KiB cap), `acp_adapter/gateway_media.py`
(staging images for admission, remote image fetch, the submit fingerprint). Our
`safety/url_policy.py`, `safety/safe_transport.py` (SSRF policy and the vetted-address client),
`gateway/intake/media.py` (how messaging turns an image into a cached file plus a note — read for
the note shape only; the ACP layer may not import the gateway), `foundation/home.secure_dir`,
`acp/paths.py`. Closed before writing.
Evidence: none.

What it does (own words): an editor's prompt is a list of ACP content blocks. Our submit carries
text only (both hosts: `SessionService.submit`, the daemon's `prompt.submit`), so the conversion
turns every block into text the model reads, and every image into a file staged in the profile's
image cache plus a note naming that file (the `vision_analyze` tool is how the agent looks at it —
the same shape the messaging intake uses):

- `text` → its text.
- `image` with base64 `data` → bytes decoded, the image signature checked (PNG/JPEG/GIF/WebP/BMP);
  over 512 KiB, or not an image → a notice instead of the file.
- `image` with only a `uri` (http/https) → fetched through the SSRF-guarded client
  (`safe_client`, every hop checked), at most 512 KiB; a refused or failed fetch → a notice naming
  the reason, the prompt still runs.
- `resource_link` to a local file (`file://`, a bare path, a Windows drive path → `/mnt/<d>/…`
  through `paths`) → an image file is staged as above (by MIME type or suffix); any other file is
  read up to 512 KiB and inlined under a header `[Attached file: name]` with its URI; a longer file
  is cut and the header says "truncated to 524288 of N bytes"; a binary file (NUL bytes, not a text
  MIME type) → "binary file omitted"; a non-file URI → a link-only note.
- `resource` (embedded) → text resources inlined the same way; blob resources decoded (base64)
  and handled as an image or text by MIME type, with the same caps.
- Images are capped by the backend's attachment limit (10, the messaging intake's
  `MAX_ATTACHMENTS`): the first ten are staged, the rest become one "N more images were not
  attached" note.

The fingerprint (`fingerprint(blocks)`) is a SHA-256 over the normalised blocks in order (kind and
the identifying fields: text, image bytes or URI, resource URI + text/blob), taken **before** any
fetch or staging: the editor's retry of the same prompt matches without fetching again; the same
blocks in another order give another fingerprint.

Inputs → outputs: `[ContentBlock]` → `Prepared(text, staged: [path], notes)`; `fingerprint`.

Files: `src/indus/acp/content.py`, `tests/unit/acp/test_content.py`.

Vendor names used (agent-client-protocol 0.12.1, schema v1.19.0, checked against the SDK models):
`TextContentBlock{type:"text", text}`, `ImageContentBlock{type:"image", data, mimeType, uri}`,
`ResourceContentBlock{type:"resource_link", uri, name, title, mimeType, size}`,
`EmbeddedResourceContentBlock{type:"resource", resource}`, `TextResourceContents{uri, text,
mimeType}`, `BlobResourceContents{uri, blob, mimeType}`, `AudioContentBlock` (not offered:
`promptCapabilities.audio` is false, an audio block becomes a note).

Edge cases / failure modes:
- empty prompt → empty text (the prompt handler refuses it before submitting);
- unreadable file → "could not read" note; missing file → same;
- base64 that does not decode → notice; an image whose bytes are not an image → notice;
- a remote image refused by the SSRF policy (private, loopback, metadata address) → notice with
  the policy's reason, nothing fetched;
- staging writes `cache/images/acp_<random><ext>` (folder like the home, file 0600, created
  exclusively).

Our design differences / decisions:
- Text-only submit: images reach the model as staged files + notes (the reference staged bytes for
  an admission that carried attachments). Same idea as our messaging intake.
- Remote image fetch failures degrade to a note, as in the reference.
- Size cap 512 KiB for every read and every image (one constant).

Tests we will write:
- 600 KiB text file truncated with the note; image link staged under the home; oversize image →
  notice; binary file omitted; non-file URI note; embedded text and blob;
- private-IP image URL refused by the SSRF policy (nothing fetched); fetch through an injected
  transport;
- same blocks → same fingerprint, reordered → different; more than ten images capped.

## Decisions for the lead
- Attachment cap: 10 (the messaging intake's `MAX_ATTACHMENTS`); extra images become a note
  rather than a refused prompt (the reference refused with `acp_content_invalid`). Easy to flip.
- Images are staged under `<home>/cache/images` and named in the text, because neither host's
  submit carries attachments today.

Compare checklist (filled after the rewrite; reopened `content.py`, `gateway_media.py`):
- [x] text / image / resource link / embedded text / embedded blob paths (test: `tests/unit/acp/test_content.py::test_base64_images_and_embedded_resources`, `::test_small_text_file_is_inlined`)
- [x] file URI forms incl. Windows drive and non-local hosts (test: `test_content.py::test_windows_drive_link_maps_to_mnt`)
- [x] truncation note and binary omission (test: `test_content.py::test_large_text_file_is_cut_with_a_note`, `::test_binary_files_and_remote_links_are_notes`)
- [x] oversize image and non-image bytes become notes (test: `test_content.py::test_oversize_and_fake_images_become_notices`)
- [x] remote image through the SSRF policy, failure → text (test: `test_content.py::test_private_image_url_is_refused_by_the_policy`, `::test_remote_images_are_fetched_through_the_given_fetcher`)
- [x] fingerprint before staging (test: `test_content.py::test_fingerprint_is_stable_and_order_sensitive`)
- [x] attachment cap (test: `test_content.py::test_more_than_ten_images_are_capped`)
- Deliberate differences: staged files + notes instead of attachment parts (text-only submit);
  over-cap images become a note instead of refusing the prompt; a non-UTF-8 file without a text
  MIME type is "binary" (the reference decoded it as Latin-1); the display name is the title, else
  the name (the reference joined both).
- Not taken: audio blocks (not advertised).

## Status

Done (2026-10-20), wave-30 builder A2. `src/indus/acp/content.py`, `tests/unit/acp/test_content.py`;
used by `prompts.py`.
