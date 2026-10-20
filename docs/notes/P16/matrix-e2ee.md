# Task: Matrix end-to-end encryption (P16-07) Phase: 16 Owner: wave-27 builder E12

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/matrix/adapter_crypto.py`
(all: the state-store shim), `plugins/platforms/matrix/adapter.py` (skim of the E2EE parts only: the
install hint and dependency probe, mode resolution, store directory per profile, the connect-time
setup — device-change reset, pickle-key migration, server key check and re-upload, `share_keys`,
recovery-key verification or bootstrap with a one-time 0600 output file —, encrypted media up and
down, the "dropping encrypted messages" warning); test names only in
`tests/gateway/test_matrix_crypto_store_per_profile.py`, `test_matrix_recovery_key_scope.py`,
`test_matrix_encrypted_drop_warning.py`. Evidence: none.
Library (dependency, not reference): mautrix-python 0.21.1 `mautrix.crypto` public API
(`OlmMachine`, `PgCryptoStore`, `mautrix.util.async_db.Database`, `TrustState`, `HTTPAPI.request`,
`mautrix.crypto.attachments`), read to call it correctly. Interop: Matrix Client-Server API v1.11
end-to-end encryption module (`m.olm.v1.curve25519-aes-sha2`, `m.megolm.v1.aes-sha2`,
`/keys/upload|query|claim`, `/sendToDevice`, `device_lists`, `device_one_time_keys_count`,
`m.room.encryption`, encrypted attachments `file` = `{url, key, iv, hashes, v}`).

What it does (own words): with `platforms.matrix.e2ee: optional | required` the Matrix adapter
reads and writes encrypted rooms. Olm/Megolm come from mautrix's crypto layer (which wraps libolm
through python-olm); everything around it is ours: the device store lives in
`<profile home>/platforms/matrix/crypto` (directory 0700, files 0600, SQLite), is locked by one
process at a time, and is reset when the access token belongs to a new device. At start the
device's keys are checked against the homeserver (uploaded when missing, refused when the server
holds other keys for the device) and one-time keys are topped up. When the profile's secret store
holds `MATRIX_RECOVERY_KEY`, the bot's device is cross-signed with it (cross-signing keys fetched
from secret storage); nothing else ever supplies that key. Each sync's to-device events, device-list
changes and one-time-key counts go to the crypto layer before the room timelines, so a room key
that arrives in the same sync as its message decrypts it. Room keys are shared only with devices
that are verified (signed by their owner's cross-signing key, or verified outright) of users on the
Matrix allowlists (or paired, or everyone under allow-all); an unverified device, or any device of
an unlisted user, never receives a key. A message that cannot be decrypted is never passed on: the
room gets one short "could not decrypt" notice per Megolm session. Attachments in encrypted rooms
are encrypted before upload and decrypted after download.

Inputs → outputs: `MatrixAdapter` + its `MatrixClient` (the `EndToEnd` seam), `MatrixOptions`
(`e2ee`, allowlists), profile scope (home, secret store) → an `EndToEnd` object:
`absorb_sync(sync)`, `decrypt(room, event) -> clear | None`, `is_encrypted(room)`,
`encrypt(room, type, content) -> (m.room.encrypted, ciphertext content)`,
`encrypt_attachment(bytes) -> (ciphertext, file info)`, `decrypt_attachment(bytes, info)`, `aclose()`.

Files: `src/indus/gateway/adapters/matrix/crypto.py` (policy, store, notice, the mautrix engine),
`tests/unit/gateway/adapters/matrix/test_crypto.py`; shared in the Matrix folder:
`adapter.py` (async `is_encrypted`, encrypted attachments in/out, `scope` property, install hint),
`client.py` (`request`, `joined_members`), `tests/fakes/platform/matrix_wire.py` (joined members,
to-device and the keys API for the extras lane), `tests/unit/gateway/adapters/matrix/test_adapter.py`
(hint wording); `pyproject.toml` (`matrix-e2ee` extra), `THIRD_PARTY_NOTICES.md`.

Edge cases / failure modes:
- Extra missing, or not Linux: `required` → connect fails fatal (`e2ee_unavailable`) with the install
  hint; `optional` → one warning, encrypted rooms skipped, plain rooms work, the gateway keeps
  running.
- Store held by another process (a running gateway while `indus send` runs, or the reverse):
  daemon connect fails retryable (`e2ee_store_busy`); a send-only adapter refuses to send into an
  encrypted room with a clear error and never sends plaintext there.
- Token for another device than the store's: the store is reset (old Olm identity cannot act for
  the new device), with a warning.
- Server holds other identity keys for our device and ours were already published: refuse (the
  store is stale), naming the directory to delete.
- Recovery key wrong or secret storage missing: warning, E2EE still runs (the device is just not
  cross-signed); the key never appears in a log, an error or a repr.
- Undecryptable event (no session, bad ciphertext, replayed index): dropped, one notice per session
  id (bounded memory), never an empty-text event.
- A room's member list or the allowlist grows: the outbound session is rotated before the next
  message so new allowed devices get a key; membership changes rotate it too.
- Send-only (`indus send`): the store is opened for sending only; no sync, no to-device handling,
  no key upload, no cross-signing; room encryption is read from room state.

Our design differences / decisions:
- Trust rule (the phase's): keys only to verified devices of allowed users. "Verified" = mautrix
  trust `CROSS_SIGNED_TOFU` or higher by default (the device is signed by its owner's
  cross-signing key); `platforms.matrix.e2ee_trust: verified` raises it to devices whose owner we
  verified. The reference shares with every device (`UNVERIFIED`).
- Recovery key only from the secret store (`MATRIX_RECOVERY_KEY`), never env, config or a file;
  no automatic bootstrap that generates a new recovery key (the reference writes one to a 0600
  file): bootstrapping belongs to the setup helper (P16-31) where the key can be shown once.
- One HTTP stack: mautrix's requests go through our httpx `MatrixClient` (a small `HTTPAPI`
  subclass), so the token handling, redirects off and the wire fake are the same as plain Matrix.
- The store is locked per process (`fcntl.flock`); the reference had no lock.
- Pickle key: derived from user and device (as the reference); the files are protected by 0700/0600.
- Not taken: pickle-key migration of pre-existing stores (no older Indus stores exist), legacy
  `crypto_store.pickle` removal, password login, key backup, interactive (SAS) verification, key
  forwarding to other users (only our own verified devices may request keys, mautrix's default).

Tests we will write:
- fake engine: unverified device gets no keys; unlisted user's verified device gets none; allowed
  verified device does; allowlist growth rotates the session;
- undecryptable event → one notice per session, nothing emitted, never empty text;
- missing extra → `required` fatal with hint, `optional` runs plain; gateway (daemon) keeps running;
- store dir 0700, files 0600, under the profile home; lock refuses a second opener;
- device change resets the store; recovery key read only from the secret store, never logged;
- send-only: no sync, no absorb, no key upload; encrypted room without the store refuses to send;
- encrypted attachments out (no plain `url`) and in (fetcher decrypts);
- extras lane (skipped without mautrix/olm): two real OlmMachines over the fake homeserver — a
  verified allowed device decrypts the bot's reply, an unverified one gets no key; recovery-key
  cross-signing.

## Decisions for the lead
- Library: `mautrix[encryption]==0.21.1` (MPL-2.0, unmodified) + `python-olm==3.2.16` (Apache-2.0,
  bundles libolm Apache-2.0). python-olm has no cp313 wheel: `uv sync --extra matrix-e2ee` builds it
  from the sdist (needs cmake and a C++ compiler, ~3 min). libolm is deprecated upstream (2024) with
  published timing side-channel advisories; vodozemac (maintained, wheels) is used by matrix-nio 0.26
  but matrix-nio has no cross-signing / recovery-key support, which this task needs. Follow-up: move
  to a vodozemac-based engine when mautrix offers one (the engine is one class behind our own
  interface).
- Extra `matrix-e2ee` Linux-only (markers `sys_platform == 'linux'`), exact pins; lead re-locks.
- `e2ee_trust` option name and default (above); `MATRIX_RECOVERY_KEY` secret name (vendor-style,
  as the reference); store path `<home>/platforms/matrix/crypto`.
- Registry: the Matrix row's `check_fn` stays `httpx` (plain Matrix needs nothing); the install hint
  could mention the extra: proposed `install_hint="pip install indus-agent (Matrix needs no extra;
  end-to-end encryption: pip install 'indus-agent[matrix-e2ee]' on Linux)"`. No credential change
  (the recovery key is not an enabling credential; it is read only by the crypto module and added
  to the adapter's scrub list).
- `indus send` into an encrypted room while the gateway runs is refused (the gateway owns the
  device keys). Follow-up: route such sends through the running daemon.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/matrix/adapter_crypto.py` and
the E2EE parts of `plugins/platforms/matrix/adapter.py`):
- [x] Install hint and the two modes: `required` refuses to connect without the libraries, `optional`
  warns and runs plain (`test_crypto.py::test_without_the_extra_required_is_fatal_with_a_hint_and_optional_runs_plain`,
  `test_adapter.py::test_required_e2ee_without_the_module_is_fatal_with_a_hint`).
- [x] Store per profile home, never shared between profiles
  (`test_crypto.py::test_the_store_lives_under_the_profile_home_with_private_modes`).
- [x] The token's device wins over a configured one; a new device resets the Olm account
  (`test_crypto_mautrix.py::test_a_new_device_resets_the_store_and_a_stale_store_is_refused`).
- [x] Server key check: missing keys are uploaded, other keys with a published store refuse (same test).
- [x] Recovery key from the profile's own secrets, failure is a warning, never logged
  (`test_crypto.py::test_the_recovery_key_comes_only_from_the_secret_store`); cross-signing really
  happens with the library (`test_crypto_mautrix.py::test_end_to_end_over_the_fake_homeserver`).
- [x] Encryption state from room state when unknown (the reference's state-store shim)
  (`test_crypto.py::test_send_only_never_syncs_or_uploads_keys`).
- [x] Encrypted attachments out and in (`test_crypto.py::test_attachments_in_encrypted_rooms_travel_encrypted`,
  and through libolm in the extras lane).
- [x] Gap found: the plain adapter started the encryption layer before it knew the token's user and
  device — the identity is now adopted first (`adapter._adopt_identity`).
- [x] Gap found: an encrypted room's state read with an empty state key matched no route in the fake
  and the layer read "not found" as "plain room" (a send could have gone out in clear) — only
  `M_NOT_FOUND` now means plain; anything else fails the send (fake route fixed; send-only test).
- [x] Gap found: undecryptable events would have notified for the startup backlog; events before the
  listener started never cause a notice (`test_crypto.py::test_backlog_and_own_events_cause_no_notice`).
- Deliberate differences: keys only to verified devices of allowed users (reference: every device);
  no automatic recovery-key bootstrap or one-time key file (P16-31 setup helper); a stale store is
  refused with advice instead of deleting the device on the server (needs interactive auth); the
  device store is locked per process; mautrix's HTTP goes through our httpx client; an event whose
  key is late waits up to three syncs before the notice; the device id must come from `whoami`
  (the reference also guessed it from a single-device key query).
- Not taken: pickle-key migration and legacy pickle removal (no older Indus stores exist); the
  re-query after key upload (the upload answer is enough); password login; key backup; SAS
  verification; forwarding room keys to other users.

## Status

Done (2026-10-20). New: `src/indus/gateway/adapters/matrix/crypto.py`,
`tests/unit/gateway/adapters/matrix/test_crypto.py` (fake engine, runs without the extra),
`tests/unit/gateway/adapters/matrix/test_crypto_mautrix.py` (real mautrix + libolm; skipped without the
extra). Shared, Matrix folder: `adapter.py` (async `is_encrypted`, encrypted attachments, `scope`,
`take_late_event`, `e2ee_trust`, identity before E2EE, install hint), `client.py` (`request`,
`joined_members`), `tests/fakes/platform/matrix_wire.py` (joined members, encryption state, keys API,
to-device, per-user tokens and account data; empty state keys), `test_adapter.py` (hint wording).
Shared elsewhere: `pyproject.toml` (`matrix-e2ee` extra), `THIRD_PARTY_NOTICES.md`,
`docs/internals/acceptance-map.md` (ACC-K10-1). The extras lane passed in a scratch overlay of the
extra's packages (built python-olm from the sdist); the lead re-locks.

Landed at integration (wave 27): the Matrix install hint names the extra (D-16.524 … D-16.527); `uv.lock` was not re-locked at integration (offline cache incomplete, see LEAD-NOTES).
