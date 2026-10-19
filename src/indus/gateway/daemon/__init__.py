"""The gateway daemon: one process per profile (P15-24 … P15-29).

``process.GatewayDaemon`` assembles and runs it; ``parts`` is how other features plug in;
``liveness``, ``status``, ``lifecycle``, ``drain``, ``watchdogs`` and ``recovery`` are its pieces;
``side_services`` (the control socket part) and ``reconnect`` are the side services.
Shared records (pid file, status file, planned stop, exit codes) live in
``indus.state.gateway_runtime``.
"""
