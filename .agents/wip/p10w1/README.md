# P10 wave 1 — slice reports

## Slice H — w15-p10h.patch (base 85cd8a9, before P09 w1 landed) — DONE (P10-08..11 execute_code)
- New: tools/builtin/code/{__init__,settings,output,launch,rpc,gate,runner,stub_module,schema,kernel,
  kernel_runner,remote,remote_runner}.py; tests/unit/tools/builtin/code/* (golden cli_description.txt);
  docs/notes/P10/code-{runner-rpc,schema-module,kernel,remote}.md.
- Shared: tools/builtin/__init__.py (BUILTIN_MODULES row); tools/toolsets.py (`code_execution`
  toolset, execute_code in CORE_TOOLS) + test; core/tool_round.py handler_extras (tool_registry,
  tool_hooks, enabled_tools); config `code_execution` section + test; test_wizard.py (core bundle +
  code_execution — P09 w1 also edited this list: union); tests/golden/prompt_surface/*/tools.json
  (regenerate after merge); traps R02.2/21/22/32; ACC-C35-1..3, E9-1.
- Wiring: host exit `await kernel.POOL.shutdown()` and `await remote.POOL.shutdown()`; long-lived
  hosts run `kernel.run_reaper(clock, idle_fn)`; /new and session close `await
  kernel.dispose_session(id)`; G (P10-06) passes `code_kernel_owner=kernel.child_owner(parent, child)`
  and calls `await kernel.dispose_child(child_id)` at teardown.
- Decisions: E9 approval (interactive asks once per session — the reference never asks; unattended
  follows approvals.*_mode so `indus -z` refuses by default); kernel auto/off; ToolCallRefused
  exception; empty tool intersection → helpers only; pinned child kernels exempt from idle sweep;
  remote `python` ≥3.8 fallback; 0.5 s remote poll serves both results and tool requests.
- Follow-ups: public Environment.temp_dir; sweep crashed-host staging dirs >7 d; move RemoteShell to
  tests/fakes/backend/.

## Slice G — w15-p10g.patch (base 85cd8a9) — DONE (P10-01..07 delegation); uv.lock EXCLUDED — re-lock
- New: tools/builtin/delegate.py; core/subagents/{__init__,delegation,child,batch,output_schema,
  background,teardown,worktree}.py; tests/unit/core/subagents/*, tests/unit/tools/builtin/test_delegate.py,
  tests/fakes/subagents.py, tests/integration/core/test_delegate_batch.py (M2 demo);
  tests/unit/core/subagents/_stubs_g.py (STAND-IN for H's kernel pool — delete); docs/notes/P10/ ×7.
- Shared: core/agent.py (AgentOptions.only_tools, delegation_depth; lazy delegate executor);
  core/turn_state.py (max_child_calls from delegation.max_concurrent_children); tools/builtin/__init__.py;
  tools/toolsets.py (`delegation` toolset, delegate_task in CORE_TOOLS — H and P09 w1 also edit);
  config DelegationSection + test; backends/processes.py (handoff grants, transfer_ownership,
  handoff_note); tools/builtin/process_manage.py (`handoff` live, `purpose` arg) + test;
  service/session_service.py (deliver_internal, stop_subagents); cli/slash/port.py (/stop, /new stop
  subagents); goldens tests/golden/prompt_surface/*/tools.json (regenerate after merge);
  test_toolsets.py, test_wizard.py (union with H and P09 w1); pyproject.toml jsonschema==4.26.0
  (MIT, 2026-01-07; pulls attrs, referencing, jsonschema-specifications, rpds-py — all MIT);
  THIRD_PARTY_NOTICES.md; traps R01.2/4/16, R02.17/32; ACC-C36-1.
- Wiring: H's kernel pool registers via core.subagents.teardown.register_releaser(...) and uses
  child_kernel_key (H's report: child passes code_kernel_owner=kernel.child_owner(parent, child),
  dispose_child at teardown) — reconcile the two names into one; then delete _stubs_g.py.
- Decisions: delegate_task in CORE_TOOLS; background only with a delivering host + interactive
  context; usage rolled up on the parent ledger only; hand-off `purpose` arg; steer via P04 steer row;
  async cap queues (reference rejects); images listed as paths/URLs until P10-15; /stop count
  includes subagents.
- Follow-ups: no subagent.* protocol events yet; hand-off note not shown in completion text; Docker
  container aliasing for children.

## Slice I — w15-p10i.patch (base 7faff19) — DONE (P10-12..14 browser); uv.lock EXCLUDED — re-lock
- New: tools/builtin/browser/{__init__,session,driver,tools,snapshot,cdp,vision}.py,
  cli/slash/browser_cmds.py, tests/fakes/browser.py, tests/fixtures/web/{index,form}.html, pixel.svg,
  tests/unit/tools/builtin/browser/*, tests/integration/tools/test_browser_local.py (real Chromium,
  marker `browser`, local server), docs/notes/P10/browser-{session,page-tools,cdp-vision}.md.
- Shared: pyproject extra `browser = ["playwright==1.63.0"]` (Apache-2.0, 2026-09-15; pulls greenlet
  3.5.6, pyee 13.0.1) + `browser` marker; THIRD_PARTY_NOTICES (3 rows); toolsets.py (BROWSER_TOOLS,
  `browser` toolset in CORE, gated on Playwright); tools/builtin/__init__.py; config `browser` section +
  test; service/commands/registry.py (/browser `use`); cli/slash/dispatch.py; test_registry.py
  (/browser landed); test_toolsets.py; test_wizard.py (core "browser" row); goldens (regenerate);
  ACC-C21-1/2, C23-1, C26-1; trap R02.22.
- SECURITY — MUST FIX AT INTEGRATION: page-initiated sub-resource requests and redirect chains through
  a private address bypass the P05 URL check (SSRF). Add a Playwright route filter that runs every
  request (and each redirect hop) through the P05 check, with tests against a local server that
  redirects to / embeds a private address.
- Wiring: core/tool_round.handler_extras offers `aux` and `main_vision`; host exit `await
  indus.tools.builtin.browser.DEFAULT_TOOLS.manager.shutdown()`; G's child teardown calls
  `DEFAULT_TOOLS.manager.release(task_id)` (register as a releaser).
- Decisions: Playwright in-process, one Chromium per task, sandbox on where possible; P05 URL check on
  the local browser too (private needs security.allow_private_urls, ?token= refused); own outline
  walker; browser_cdp without target_id/frame_id; viewport screenshots; /browser connect never
  launches; no cloud/alt backends. Pinned 1.63.0 expects Chromium build 1243 (machine has 1194):
  test falls back to $PLAYWRIGHT_BROWSERS_PATH/chromium; users set browser.executable_path.
- Follow-up: screenshot embed-size budget (P10-15).

## Slice J — w15-p10j.patch (base 7faff19) — DONE (P10-15..18 vision, image/video, TTS, STT); uv.lock EXCLUDED
- New: tools/builtin/media/{__init__,vision,providers,generate,tts,stt,imaging,sources,common,audio}.py,
  tests/unit/tools/builtin/media/*, tests/fakes/media_providers.py, docs/notes/P10/{vision-analyze,
  media-generate,tts,stt}.md.
- Shared: tools/builtin/__init__.py; toolsets.py (CORE += vision_analyze, image_generate,
  text_to_speech; sets image_gen, video_gen (opt-in), tts); config sections vision, image_gen, media,
  tts, stt + test; pyproject extra `voice = ["faster-whisper==1.2.1"]` (P10-19 adds sounddevice);
  test_toolsets.py, cli/commands/test_tools.py, test_wizard.py; goldens (regenerate); docs/notes/P04/
  golden-prompts.md; traps R06.22, R06.23; ACC-C27-1.
- Wiring: tool_round.handler_extras offers `aux`, `native_vision` (route supports_vision and wire
  carries images in tool results), `subagent` (true in G's children) — same `aux` extra I needs;
  TTS hosts may pass `platform`, `voice`; Docker: mount <home>/cache/media into the sandbox (P11
  backends/docker) or drop `sandbox_path`.
- LICENCE / OWNER items: Pillow not added (proposal: extra `vision = ["pillow==12.3.0"]`, licence
  MIT-CMU not yet on the allow-list); Piper TTS runs as a separate process (bundles eSpeak NG GPL-3.0;
  same ruling as signal-cli, D-16.71); faster-whisper extra pulls `av` whose wheels bundle LGPL FFmpeg
  libs — licence check only walks main deps, confirm by hand.
- Intended differences (need D-ids): over child embed cap → text route; WAV-only silence cutting;
  vendor-neutral video_edit/extend; one TTS file per call; no [[audio_as_voice]] marker
  (voice_compatible field); no SVG/HEIC conversion. Config choices: media.video_provider/video_model,
  image_gen.key_name / tts.key_name default OPENAI_API_KEY, tts.provider piper, stt.max_file_mb 25.
- Follow-up: aux TASK_TIMEOUTS vision: 120.
