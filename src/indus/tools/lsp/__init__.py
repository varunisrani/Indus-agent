"""Language servers for edits (P24-15, P24-16).

- ``servers``: the catalogue (which server handles a file, its root markers, install hints);
- ``client``: one server over stdio (LSP 3.17), versions, diagnostics, shutdown escalation;
- ``manager``: one server per (server, project root), trust and broken sets, idle reaping;
- ``diagnostics``: errors an edit introduced, inline within the write budget or later as notes
  for the editing session, and the per-turn list of edited files for the stop gates.

A host turns the tier on once per process::

    manager = LspManager(scope=scope)
    diagnostics.install_active(EditDiagnostics(manager))

and wraps its tool hook seam with ``diagnostics.LspToolHooks``. Nothing here is imported at
boot; ``tools.builtin.lint`` loads ``diagnostics`` only when a checker is installed.
"""
