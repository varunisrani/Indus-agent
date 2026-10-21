"""One gateway process serving several profiles (P24-01..04, D-16.17: off by default).

``host``: the per-user host lock, the rendezvous record, the second-run attach verdict and the
serve/unserve/rescan verbs. ``scope``: each served profile's ``Scope`` and the fail-closed reader
for credentials and allowlists. ``routes``: profile routes and the two adapter resolvers (which bot
received a message, which bot answers it). ``ingress``: secondary profiles' HTTP apps mounted on
the default profile's listener under ``/p/<profile>/``.
"""
