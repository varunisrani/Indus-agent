Sample dashboard plugins for `tests/contract/web/test_dashboard_plugins.py` (P24-37), written by us.

- `hello/` — a tab with a script, a stylesheet, slots and a Python `api` router (`GET /ping`).
- `notes/` — overrides the built-in Logs page; no backend.
- `takeover/` — tries to override `/plugins` (refused).
- `sneaky/` — an `api` path that climbs out of its folder (backend refused, assets still served).
