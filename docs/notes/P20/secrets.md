# Task: Secrets at rest (P20-07) Phase: 20 Owner: wave-33 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/secret-storage-policy.ts`
(all), `apps/desktop/electron/hardening.ts` (file-permission parts: owner-only mode, atomic write,
the safeStorage encode/decode seam). Research 08 §3.28 (secrets opt-in in the reference), §3.30;
traps R08.10; D-16.13. Closed before writing.
Evidence: none.

What it does (own words): the desktop keeps very few secrets itself — the saved remote connection's
token and (later, N17) access-proxy headers. Provider API keys never live here: they go to the
profile's `SecretStore` through the daemon/REST API, and the store refuses any name outside its
short allow-list. By default the desktop's secrets are encrypted with the OS keychain through
Electron `safeStorage` (D-16.13 flips the reference's opt-in). Before anything touches
`safeStorage`, a probe decides whether the keychain is usable this run, without ever asking the
user: on macOS it runs `security show-keychain-info` on the default keychain in a child process
(no UI, a locked or missing keychain is an error) and only then calls `isEncryptionAvailable`; on
Linux the selected backend must be a real secret service (`basic_text` counts as unusable); on
Windows DPAPI never prompts. The probe has a 2,000 ms deadline; throwing, hanging or any error marks
the keychain unusable for this run. When it is unusable, secrets are written as owner-only files
(0600) in a 0700 directory under userData, and keychain-encrypted entries already on disk read as
absent — no prompt, ever. `secrets-policy.json` records the chosen store (`keychain` by default,
`file` when the person chooses it in Settings); switching re-writes every entry in the new store.

Inputs → outputs:
- `probeKeychain({ platform, safeStorage, runSecurity, timeoutMs })` → `{ usable, reason }`.
- `DesktopSecrets.open({ dir, policyPath, probe })` → `{ get(name), put(name, value),
  delete(name), policy(), setStore("keychain" | "file") }`.
- entry file `<userData>/secrets/<name>.json` (`0600`): `{ v: 1, store: "keychain", data: base64 }`
  or `{ v: 1, store: "file", data: "<value>" }`; directory `0700`.
- `secrets-policy.json` `{ v: 1, store, reason: "default" | "user" }`.

Files: `apps/desktop/electron/secrets.ts`, `apps/desktop/electron/secrets.test.ts`,
`apps/desktop/electron/fsSecure.ts` (owner-only atomic write, shared with P20-09/20/26 files).

Edge cases / failure modes:
- probe throws, hangs past 2 s, or `security` exits non-zero → file fallback; files 0600, dir 0700;
- keychain works → the value never appears in plaintext in any file under userData
  (`connection.json` holds only a reference);
- keychain entry on disk but keychain unusable now → `get` answers null, `safeStorage` untouched;
- decrypt fails (keychain reset) → null, entry kept (it may decrypt next run), logged once;
- unknown name (`openai_api_key`) → refused (`not-a-desktop-secret`);
- empty value → `delete`;
- directory exists with loose bits → tightened to 0700; a symlink at an entry path → refused, not
  followed; Windows → mode bits skipped (ACL'd userData);
- switching to keychain while unusable → refused with a reason the Settings toggle shows.

Our design differences / decisions:
- Keychain by default (D-16.13); the reference defaults to plain files because a touch can prompt.
  We avoid the prompt with the probe, not with the default.
- No legacy-blob migration pass (no older Indus builds exist).
- Name allow-list: `remote-connection-token`, `access-proxy-headers`.

Tests we will write:
- probe throwing / hanging (fake timers) / security exit 1 → unusable; ok → usable; Linux
  `basic_text` → unusable.
- file fallback: put/get/delete; stat dir 0700, files 0600; tighten loose dir; symlink refused.
- keychain mode (mocked safeStorage): no plaintext anywhere under userData, a stand-in
  `connection.json` holds only the reference; round trip.
- keychain entry + unusable keychain → null without calling safeStorage.
- policy file: default keychain; set file migrates entries; set keychain while unusable refused.
- non-desktop names refused.

## Decisions for the lead
- macOS probe command: `/usr/bin/security show-keychain-info` (default keychain) with
  `stdio: ignore`, 2 s deadline, killed on timeout.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Probe throwing / hanging 2 s (fake timers) / macOS check failing → file fallback without touching `safeStorage`; Linux `basic_text` unusable (test: `electron/secrets.test.ts::keychain probe`).
- [x] Files 0600 and dir 0700 (stat), loose dir tightened, symlinked entry refused (test: `file fallback`).
- [x] No secret in `connection.json` or any file in plaintext when the keychain works (mocked `safeStorage`), round trip (test: `keychain store`).
- [x] Policy file default/user choice, migration on switch, keychain refused while unusable, provider-key names refused (test: `policy`).
- [x] Compared with `secret-storage-policy.ts` and the hardening file-mode helpers: strict enum parsing of the policy (their `=== true` rule), owner-only atomic writes with the temp file created 0600 (their chmod-before-rename point), no symlink following.
- Deliberate differences: keychain by default with a prompt-free probe (D-16.13) instead of opt-in; no legacy-blob migration (no older Indus builds); a name allow-list keeps provider keys out.
- Not taken: per-save plain-text confirm dialog (file mode is owner-only by design), `--password-store=basic` plain-text encryption switch (basic_text counts as unusable → owner-only files).

## Status

Done (2026-10-21). Wired in `main.ts` (probe on ready, `installSecretsIpc`). P20-11 stores the remote token through `DesktopSecrets.put("remote-connection-token", …)` and keeps only the name in `connection.json`.
