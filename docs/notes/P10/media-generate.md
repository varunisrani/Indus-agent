# Task: Image and video generation (P10-16) Phase: 10 Owner: P10 slice J builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/image_generation_tool.py`
(provider routing, result shape, cache path translation), `tools/video_generation_tool.py` (dynamic
schema, provider call), `tools/xai_video_tools.py` (skim: edit/extend semantics),
`agent/image_gen_provider.py` / `agent/video_gen_provider.py` (skim: the provider contract).
Closed before writing.
Evidence: none.

What it does (own words): image and video generation go through providers kept in two registries
(`image_providers`, `video_providers`). A provider says who it is and what it can do (aspect
ratios, editing an input image, reference images, durations, resolutions, video edit, video extend)
and turns a request into a `MediaResult`: a URL, a file path, or bytes we save. Which provider and
which model are the user's choice (`image_gen.provider` / `image_gen.model`,
`media.video_provider` / `media.video_model`); a `model` argument from the agent is ignored. One
built-in image provider talks to an OpenAI-compatible images endpoint with the key from the secret
store. At most `image_gen.max_parallel_requests` (4) image requests run at once per profile.

`video_generate`, `video_edit` and `video_extend` exist only while some registered video provider
declares the capability, and their descriptions are rebuilt from the active provider's
capabilities. They live in the opt-in toolset `video_gen`; `image_generate` is in `image_gen` and
in the core list. Saved results land under `<home>/cache/media/` with an absolute path; when the
terminal runs in Docker the result also names where that file sits inside the sandbox.

Inputs → outputs: `image_generate {prompt, aspect_ratio, image_url?, reference_image_urls?}`,
`video_generate {prompt, image_url?, duration?, aspect_ratio?, resolution?}`,
`video_edit|video_extend {video_url, prompt}` → JSON `{success, image|video, provider, model,
sandbox_path?}` or `{success: false, error, error_type}`.

Files: `src/indus/tools/builtin/media/providers.py`, `media/generate.py`,
`tests/unit/tools/builtin/media/test_generate.py`, `tests/fakes/media_providers.py`.
Shared: `src/indus/tools/toolsets.py` (`image_gen`, `video_gen`, core gets `image_generate`),
`src/indus/foundation/config/schema.py` (`image_gen`, `media`) and its test.

Edge cases / failure modes:
- no provider configured / unknown name → error naming the known providers;
- provider raises → `provider_error` with its message; returns the wrong type → `provider_contract`;
- edit input or references on a provider that does not declare them → plain refusal before the call;
- input images are loaded with the vision loader rules (URL policy, secret-file guard, 50 MB);
- more reference images than declared → refused with the limit;
- aspect ratio outside the provider's list → the provider default;
- a provider registered after tools were built: the video tools are re-synced on every registry
  the media tools were registered into.

Our design differences / decisions:
- Vendor-neutral names `video_edit` / `video_extend` (naming §a8).
- No in-tree video provider; no FAL / managed gateway; no upscaler.
- Image requests marked read-only for the planner (they only create new cache files), so a batch
  of image calls runs in parallel under the semaphore.
- Docker path translation assumes `<home>` is visible as `/root/.indus` in the sandbox; the cache
  mount itself is a P11 follow-up (see below).

Tests we will write:
- fake provider: image bytes saved under `cache/media/`, absolute path returned;
- agent `model` argument ignored, configured model passed;
- 5 parallel calls → ≤ 4 in flight;
- no video provider → no video tools; provider with edit → `video_edit` present with capability
  text; profile whose active provider lacks edit does not see it (probe);
- built-in OpenAI-compatible provider against respx (generation and edit, key from secrets).

## Decisions for the lead
- `media.video_provider` / `media.video_model` hold the video choice (the phase lists a `media`
  section, no `video_gen` one).
- Docker: no mount carries `<home>/cache/media` into the sandbox yet. `sandbox_path` assumes
  `/root/.indus/cache/media/...`; add a read-only mount in `backends/docker` (P11 owner) or drop
  the field. **Integration (lead):** mounted — `backends/docker/environment.media_mounts` binds
  the folder read-only at `/root/.indus/cache/media` (created when missing); test
  `tests/unit/backends/docker/test_mounts.py::test_media_cache_is_mounted_where_the_media_tools_say`.
- Built-in image model default `gpt-image-1`; base URL default `https://api.openai.com/v1`, key
  name `OPENAI_API_KEY` (config `image_gen.key_name`).

Compare checklist (filled after the rewrite):
- [x] model chosen by config only (test: `test_generate.py::test_agent_model_ignored`)
- [x] capability-gated optional params and descriptions (tests: `test_video_*`)
- [x] provider error / contract error shapes (test: `test_provider_errors_and_results`)
- [x] cache path translation for container backends (test: `test_docker_sandbox_path`)
- Deliberate differences: separate edit/extend tools behind capabilities (the reference keeps them
  vendor-specific); no FAL fallback.
- Not taken: upscaling, creative controls, managed-gateway routing, provider model catalogues.

## Status

Done (2026-10-19).
