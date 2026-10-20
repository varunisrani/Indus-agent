# Wave 27 — parked builder patches

## (LANDED) w27-e11.patch: P16-31 setup wizard + P16-32 target parsers

## w27-e13.patch: P16-02 WhatsApp Node bridge sidecar + P16-03 WhatsApp bridge adapter — done (base 90209a7)
- Builder checks: bridge vitest 37 passed; pnpm 892 passed (62 files); pnpm -r typecheck clean; biome clean; pytest
  whatsapp + contract + schema 479 passed / 47 skipped (incl. the real Node bridge over loopback with the fake provider);
  config 217 passed; ruff, pyright, lint-imports, tooling.checks ok. NO new packages (Python or Node); pnpm lock unchanged.
- OWNER DECISION: Baileys (MIT) depends on libsignal-node (GPL-3.0, git URL) and a non-optional `sharp` peer with
  LGPL-3.0-or-later libvips binaries — not on licenses_allow.json. The bridge reaches WhatsApp only through our own
  WaSocket interface and loads an operator-installed provider named by INDUS_WA_PROVIDER; `bindBaileys` is our own MIT
  mapping (no build-time import). Options: (a) allow an operator-installed copyleft provider (dependency would be
  @whiskeysockets/baileys@6.7.24, installed by the operator, not shipped); (b) delete bindBaileys; (c) build a provider on
  whatsapp-web.js (Apache-2.0, drives Chromium via puppeteer).
- Wiring (core): registry `PlatformEntry("whatsapp", "WhatsApp", lazy indus.gateway.adapters.whatsapp:create_adapter,
  check_fn=modules_installed("httpx"), required_env=(), max_len=4096, dm_is_private=True)` + INDUS_WHATSAPP_* policy env;
  BUILTIN_NAMES += whatsapp before whatsapp_cloud; no credentials (enable via platforms.whatsapp.enabled: true); no lock_env
  (auth folder + pidfile ownership); hint in the note; add an `indus send` case after the row lands; target rule for
  whatsapp (P16-32 follow-up).
- Security: loopback-only local API (non-loopback host exits 2), Bearer token per start (0600 file) compared in constant
  time, Host header must be loopback, 64 KiB JSON cap, auth folder 0700 / umask 077, no message text or numbers on stdout,
  exits within 2 s of parent death; adapter adopts only its own healthy bridge, never signals others, child env minimal.
- Decisions: per-start token persisted 0600; require_mention default on; lazy media; cursor+epoch events; slot spacing
  0.3 s; provider from config/env; bridge script from config (source checkout default apps/whatsapp-bridge/src/main.ts).
- Follow-ups: packaging the bridge for wheel installs (P23/P25); P16-31 pairing flow (INDUS_WA_MODE=pair); canonical
  participant alias folding (core); bridge proxy; doctor ports 3000/3001–3999; live lane; .nvmrc Node 24 vs sandbox 22.

## w27-e12.patch: P16-07 Matrix end-to-end encryption + P16-19 Weixin (iLink Bot) — done (base 90209a7)
- Builder checks: matrix + weixin + schema + contract 520 passed / 48 skipped; delivery 122 passed; extras lane (mautrix +
  python-olm built from sdist) matrix 58 passed; ruff, pyright, lint-imports, tooling.checks ok. uv.lock not regenerated.
- Extras: `matrix-e2ee` (Linux-only, exact pins: mautrix 0.21.1 MPL-2.0 unmodified; python-olm 3.2.16 Apache-2.0 bundling
  libolm Apache-2.0; pycryptodome 3.23.0 BSD-2/public domain; unpaddedbase64 2.1.0 Apache-2.0; base58 2.1.1 MIT;
  aiosqlite 0.22.1 MIT; asyncpg 0.31.0 Apache-2.0); `weixin = ["cryptography==50.0.2"]`. Re-lock; check licences from
  dist-info; 00-layout pins.
- OWNER NOTE: libolm is deprecated upstream with published timing side-channel advisories; the engine sits behind our own
  Engine interface (move to vodozemac when mautrix supports it). python-olm has no cp313 wheel (sdist build, cmake + C++).
- Shared: matrix/adapter.py (async is_encrypted, encrypted attachments, public scope, take_late_event, e2ee_trust, token
  user/device adopted first), matrix/client.py (request(), joined_members), matrix_wire fake (keys API etc.), schema row
  platforms.weixin, conftest weixin case, ACC-K10-1, ACC-K11-4, THIRD_PARTY_NOTICES rows.
- Wiring (core): registry `_chat_entry("weixin", "Weixin / WeChat", sdk=("cryptography",), extra_name="weixin",
  required_env=("WEIXIN_TOKEN","WEIXIN_ACCOUNT_ID"), max_len=2000, hint="You are talking through Weixin (WeChat). Markdown is
  shown as typed, so keep formatting light and replies short.")` with lock_env=("WEIXIN_ACCOUNT_ID",); BUILTIN_CREDENTIALS
  weixin (TOKEN, ACCOUNT_ID); BUILTIN_NAMES += weixin after wecom_callback; cron KNOWN_PLATFORMS += weixin; target rule
  for weixin in gateway/adapters/targets.py; send test case; Matrix install hint mentions the matrix-e2ee extra.
  MATRIX_RECOVERY_KEY only from the SecretStore.
- Decisions: room keys only to verified devices of allowed users (owner cross-signing by default; e2ee_trust: verified);
  rotation on allowed-member change; late keys wait 3 syncs; store 0700/0600 + flock (second gateway → retryable
  e2ee_store_busy); `indus send` refused for encrypted rooms (never in clear); send-only does no sync/keys; mautrix HTTP via
  our httpx; no automatic recovery-key bootstrap (P16-31); Weixin never sends without a context token (-14 drops it);
  tokens/cursor 0600 under <home>/platforms/weixin/<account>/; dedupe 60 s text / 300 s ids; pacing 1.5 s + 30 s pause on
  frequency limit; inbound media https Tencent hosts only; groups off by default; ECB vendor-mandated via cryptography.
- Follow-ups: vodozemac engine; iLink names need a live check (P16-36); `indus send` into encrypted rooms via the daemon;
  Weixin QR login (P16-31).

## w27-e14.patch: P16-21 Yuanbao adapter + P16-22 Yuanbao tools + P16-30 Raft wake bridge — done (base 846a142)
- Builder checks: yuanbao + raft 66 passed / 1 skipped (real opener needs httpx-ws; passed with it); required set 690 passed;
  wider gateway/cli/config/contract/tools 4882 passed; integration + e2e gateway 50 passed; ruff, pyright, lint-imports,
  tooling.checks ok. No new packages; extra `yuanbao = ["httpx-ws==0.9.0"]` (re-lock metadata).
- Stand-in: tests/unit/gateway/adapters/yuanbao/_stubs_e14.py (registry entries + hint texts + PENDING_PLATFORMS) — remove when
  the rows land (see below).
- Shared: schema rows platforms.yuanbao / raft; targets.py Yuanbao rule (group:<code> / direct:<account>, bare numbers refused);
  gateway_setup STEPS for yuanbao/raft; conftest import; traps R02.25/R02.26/R06.3/R06.4/R06.24; ACC-K11-6, ACC-K12-11;
  THIRD_PARTY_NOTICES + 00-layout httpx-ws/wsproto rows name yuanbao.
- Wiring (core): BUILTIN_NAMES += yuanbao, raft (after buzz); registry
  `replace(_chat_entry("yuanbao", "Yuanbao", sdk=("httpx_ws",), extra_name="yuanbao", required_env=("YUANBAO_APP_ID",
  "YUANBAO_APP_SECRET"), max_len=4000, hint=<YUANBAO_ENTRY.platform_hint>), lock_env=("YUANBAO_APP_ID",))` and
  `PlatformEntry(name="raft", label="Raft", factory=_lazy_factory("indus.gateway.adapters.raft:create_adapter"),
  check_fn=modules_installed("uvicorn"), required_env=("RAFT_PROFILE",), install_hint=<RAFT_ENTRY.install_hint>,
  dm_is_private=False, platform_hint=<RAFT_ENTRY.platform_hint>, lock_env=("RAFT_PROFILE",))` (texts in _stubs_e14.py);
  BUILTIN_CREDENTIALS yuanbao (APP_ID, APP_SECRET); BUILTIN_PUBLIC_ENV raft (RAFT_PROFILE); toolsets `yuanbao` (platforms
  {yuanbao}) + `indus-yuanbao` (CORE + yuanbao) + into indus-gateway; UNTRUSTED_PREFIXES += "yuanbao_"; gateway_tool_registry
  registers yuanbao tools; cron KNOWN_PLATFORMS += yuanbao; raft in send_command and directory _NOT_TARGETS. Then delete the
  stub, drop PENDING_PLATFORMS imports, use PlatformRegistry() in yuanbao/test_tools.py and raft/test_adapter.py; send cases.
- Decisions: operator runs `raft agent bridge` (URL + token in <home>/platforms/raft/bridge.json 0600); activity telemetry
  endpoints only (report_activity seam); Yuanbao `indus send` opens its own send-only socket (kick behaviour needs live
  check); auth-bind instance id 17 (configurable); errors read from message text; no 1.5 s push merging; tools act on the
  current chat only; yuanbao_send_dm text-only, recipient must be a group member, passes the gateway Authorizer, approval
  each time (per-recipient session approval), credential shapes masked; early pushes buffered (≤200) until the sink is set.
- Follow-ups: tools' current_identity() under P17-22 tasks (session-id → route lookup); TypingLoop seam; Yuanbao live lane;
  quoted-message context; Raft /health activity summary; supervised raft bridge child.

P17-15..22 (K6..K9) LANDED as 090479b; their sections and patches were pruned.
