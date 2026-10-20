"""Clients of the profile daemon's attach endpoint (P17-15..17).

- ``channel``: the JSON-RPC channel over the loopback WebSocket (fresh ticket per dial,
  heartbeat, reconnect, ``TransportLost`` / ``NotSent``);
- ``ws_client``: the small ``wsproto`` WebSocket client the channel dials with;
- ``journal``: the prepared-submission journal (``<home>/state/attach-journal/``);
- ``host``: ``RemoteSessionService`` (the session API over the channel) and ``RemoteHost`` (its
  blocking facade for the REPL).

Finding or starting the daemon is ``indus.service.gateway_ensure``.
"""
