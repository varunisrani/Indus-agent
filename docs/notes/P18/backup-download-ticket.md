# Task: A download ticket for backup archives (P18-16 follow-up) Phase: 18 Owner: wave-33 builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — a follow-up of our own D-16.596.
Our side: `src/indus/web/auth/download_ticket.py`, `web/routes/files.py` (mint + redeem),
`web/routes/system.py` (`download_backup`), `web/auth/{gate,public_paths}.py` (`TICKET_PATHS`), the
in-flight System page (`.agents/wip/w32/w32-p1.patch`: `downloadBackup` = `saveAttachment`, a fetch
with the session header and a blob), `tests/contract/web/test_files_system_routes.py`.
Evidence: none.

What it does (own words): today the System page downloads a backup archive with a fetch that sends
the session header and saves the answer as a blob. That never puts a credential in a URL, but the
whole archive (backups can be large) sits in the tab's memory before it is saved. With a ticket the
page can hand the download to the browser like the Files page does:

- `POST /api/ops/backup/download-ticket {archive}` (behind the gate, profile-scoped like the other
  writes) checks the archive exactly as the download does (in this profile's `backups` folder, a
  `.zip`, a file) and answers `{ticket, expires_in}` with `Cache-Control: no-store`; the ticket is
  minted in the same per-process book as file tickets (`DownloadTickets`: one use, 30 s, digest
  only) bound to `backup:<resolved path>`, so a backup ticket can never fetch a file through
  `/api/files/download` and a file ticket never a backup;
- `GET /api/backups/download?archive=&ticket=` is a ticket route (`TICKET_PATHS`), machine-level
  like the files download (no `?profile=`: the ticket's bound path already names the archive the
  mint checked; outside the SPA's profile-scoped `/api/ops` family on purpose): the ticket is
  redeemed first (a bad one is one 401 `download_ticket_invalid`, nothing about the disk is
  answered), then the archive path is re-resolved and compared with the bound one;
- the existing header route `GET /api/ops/backup/download` stays as it is, so the in-flight page
  keeps working until it switches to mint + link.

Inputs → outputs: as above.

Files: shared `src/indus/web/routes/system.py`, `src/indus/web/auth/public_paths.py`
(`TICKET_PATHS` + one path), `apps/web/src/test/routes.generated.ts` (regenerated); test
`tests/contract/web/test_backup_ticket.py`.

Edge cases / failure modes: archive outside the backups folder or not a zip → 403 at mint; missing →
404 at mint; ticket reused, expired, made up, minted for a file or another archive → 401; a gated
dashboard still wants the session cookie as well (the gate), as for files; `?token=` → 401 as on
every route.

Our design differences / decisions:
- A separate redeem path (`/api/backups/download`) instead of making the header route ticket-only: the page being merged uses
  the header route, and a route that accepts either would need the gate to tell the handler which
  credential it saw.

Tests we will write: mint + redeem once; reuse refused; other archive refused; a file ticket refused
here and a backup ticket refused on the files download; no-ticket probe tells nothing (same 401 for
an unknown profile and a missing archive); outside the folder refused at mint; header route
unchanged; the ticket never in a log record.

## Decisions for the lead
- SPA switch (`downloadBackup` → mint, then a credential-free link, as `downloadFile`) is left to
  the integrator of the web patches (apps/web/src is frozen for this wave); afterwards the header
  route can go.

## Status

Done (2026-10-21), backend only; SPA follow-up above.
