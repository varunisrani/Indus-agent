# Task: `vision_analyze` (P10-15) Phase: 10 Owner: P10 slice J builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/vision_tools.py` (resize ladder,
native envelope, auxiliary path, error hints), `tools/vision_tools_history_budget.py` (all),
`tools/vision_tools_image_prep.py` (magic sniff, frame/pixel validation, region crop),
`tools/image_source.py` (data URL, http, local path, credential guard). Research `02` §3.18.
Closed before writing.
Evidence: none.

What it does (own words): the model points at an image (web address, local file or `data:` URL)
and asks a question, optionally about a rectangle of it. The image is fetched under the same URL
policy as the web tools (private and metadata addresses refused, every redirect re-checked),
capped at 50 MB and `vision.download_timeout` seconds. Its type comes from its first bytes, never
from the name: a PNG called `.jpg` is fine, a text file called `.png` is refused. Animated images
are bounded (100 frames, 100,000,000 pixels in total) before anything decodes them. A region is
cut from the original pixels first, so a small detail keeps its full resolution.

Then one of two routes. When the main model can see images (the host says so), the tool returns
the multimodal envelope: a short text part plus the image, shrunk to at most 1,568 px on the
longest side and to the embed target (`vision.embed_target_kb`, 256 KB, clamped to 64 KB–4 MB),
because that image is sent again on every later model call of the session. Otherwise the
auxiliary model (task `vision`) is asked and its answer comes back as text. Inside a delegated
child the same source may be embedded at most three times per session (region crops of one file
share the count, at most 4,096 counters kept); further calls take the text route instead of
growing the child's history. Billing, size and "model cannot see images" failures become plain
hints.

Inputs → outputs: `{image_url, question, region?}` + handler context (`scope`, `session_id`, and
from the host: `aux`, `native_vision`, `subagent`) → envelope dict, or JSON
`{success, analysis, note?}` / `{error}`.

Files: `src/indus/tools/builtin/media/__init__.py`, `media/vision.py`, `media/imaging.py` (sniff,
header facts, pure-PNG codec, Pillow when present), `media/sources.py` (loading),
`media/common.py` (settings, folders, output path rules), `tests/unit/tools/builtin/media/test_vision.py`,
`tests/unit/tools/builtin/media/_images.py` (test-only image builders).
Shared: `src/indus/tools/builtin/__init__.py` (module row), `src/indus/tools/toolsets.py` (core
membership), `src/indus/foundation/config/schema.py` (`vision` section) and its test. Shared for
all slice J tasks: `tests/unit/tools/test_toolsets.py`, `tests/unit/cli/commands/test_tools.py`
and `tests/unit/cli/setup/test_wizard.py` (core list grew), the four golden `tools.json` files plus
a regeneration entry in `docs/notes/P04/golden-prompts.md`, the traps ledger (R06.22, R06.23) and
the acceptance map (ACC-C27-1).

Edge cases / failure modes:
- empty `image_url`, bad region (not four numbers, zero area after clamping) → error naming the
  image size;
- truncated download / corrupt PNG stream → "could not be decoded";
- `file://` and `~` paths; secret files (`.env`, the Indus secret store) refused like `read_file`;
  device paths refused;
- data URL that is not base64, or bigger than the cap before decoding;
- no Pillow: PNG is handled by our own small codec; a JPEG/WebP/GIF that is already within the
  embed limits is sent unchanged; one that would need shrinking goes to the text route with a
  note naming the `vision` extra; a region on a non-PNG without Pillow → clear error;
- parallel calls on one image in a child: the reservation is one locked check-and-count; a failed
  embed gives its slot back.

Our design differences / decisions:
- Over the child cap we fall back to the auxiliary text route (the reference refuses).
- No SVG/HEIC/BMP conversion without Pillow; BMP converts only with Pillow.
- The decision "main model sees images" is the host's (`native_vision` handler keyword); the tool
  does not read the route itself (tools sit below core).
- Pillow is optional and imported lazily; our pure-PNG path keeps the core working without it.
- The repeat cap is a constant (3) rather than a config key; only children are counted.

Tests we will write:
- magic-byte typing (mislabelled PNG accepted, text-as-PNG refused), JPEG/GIF/WebP header facts,
  GIF frame cap and pixel cap;
- private URL refused before any request; redirect to a private address refused; 50 MB cap;
- 4,000 px PNG downscaled to 1,568 in the envelope; region crop pixel-exact (pure path and, when
  installed, Pillow);
- 4th embed of one source in a child → auxiliary text route; region crops share the counter;
- non-vision route → the fake auxiliary model is called with the image part; billing error hint.

## Decisions for the lead
- Wiring: the tool round should offer `aux` (the session's bound `AuxClient`), `native_vision`
  (route profile `supports_vision` and a wire that carries images in tool results) and `subagent`
  (true inside delegated children) as handler extras. Without `aux` the text route answers "no
  side-task model" in plain words.
- Pillow: proposed optional extra `vision = ["pillow==12.3.0"]` (licence MIT-CMU — not on the
  allow-list yet; it is an HPND-style permissive licence). Not added to `pyproject.toml` here
  because the task row does not name it; the code imports it lazily when present.
- Non-local terminal backends: local paths are read on the Indus host only.

Compare checklist (filled after the rewrite; reopened the four reference files):
- [x] 50 MB download cap, download timeout from config (test: `test_vision.py::test_download_cap`)
- [x] magic-byte typing, frame and pixel caps (tests: `test_png_with_wrong_extension_accepted`,
  `test_text_named_png_refused`, `test_animation_caps`, `test_truncated_png_refused`)
- [x] 1,568 px / embed target with 64 KB–4 MB clamp (tests: `test_native_downscales_4000px`,
  `test_embed_target_clamped`)
- [x] region before downscale, clamp to bounds, zero-area error names the size
  (tests: `test_region_crop_pixel_exact`, `test_region_clamped_and_zero_area`)
- [x] child cap 3 per (session, source), crops share, ≤ 4,096 keys, reserve/release
  (tests: `test_fourth_embed_in_child_goes_to_text`, `test_budget_*`)
- [x] billing/size/capability hints (test: `test_side_model_errors_become_hints`)
- [x] private URL and redirect re-check (tests: `test_private_url_refused_before_any_request`,
  `test_redirect_to_private_address_refused`)
- Deliberate differences: over-cap calls go to the text route instead of a refusal; no SVG/HEIC
  conversion; no size-error retry loop on the auxiliary call (we send a bounded image the first
  time); no "image already attached to this user turn" short-cut (no native user attachments yet).
- Not taken: `video_analyze` (not in this task), lazy installs, container-side file fallback,
  download retries with backoff, a second side-model call on an empty answer.
- Follow-up: the side-model call uses the auxiliary client's default 60 s deadline unless
  `auxiliary.vision.timeout_s` is set; the reference allows 120 s for slow local vision models —
  consider `vision: 120` in `providers/auxiliary.TASK_TIMEOUTS` (not this slice's file).
- Pillow path: run here with `pillow==12.3.0` installed temporarily (76 passed, both codecs), then
  removed again; CI without Pillow runs the pure-PNG codec cases.

## Status

Done (2026-10-19). Wiring of the three handler extras is left to the lead (see above).
