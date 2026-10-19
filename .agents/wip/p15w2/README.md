# P15 waves 2–3 — slice reports (base d0f29d0)

## W1 — w20-p15w1.patch — DONE (P15-19 delivery ledger, P15-20 deliverers/router/directory/mirror/`indus send`)
- New: state/delivery_ledger.py; gateway/delivery/{__init__,ledger,router,directory,mirror,send_command}.py;
  tests (state ledger table, gateway/delivery ×5, integration/gateway ledger kill-point recovery,
  cli test_send); notes delivery-ledger, platform-deliverers.
- Shared: state/schema.py delivery_ledger table; config gateway.delivery_ledger (default true) + test;
  outbound/sender.py (send_with_retry for every piece, ledger checkpoints, FinalReply fields),
  outbound/ports.py send_final_with_retry (plain fallback strips markup), outbound/replies.py (reply()
  session_key/message_ref; add/remove), core/admission.py (ledger key `<sid>:<turn_id>`, R06.5 gate,
  mirror notes staged per turn), adapters/base.py (ChannelEntry, list_channels → None), cli/app.py `send`
  row (string-loaded), contract help + non-TTY (send exit 2), core/_kit.py, test_sender partial-failure
  test, traps R06.5, R02.26; ACC-H10-1, K30-1.
- Daemon wiring (W3): ledger = DeliveryLedger.for_scope(scope, store) (None if disabled);
  OutboundSender(..., ledger=ledger) per platform; ledger.bind(platform_replies.sender_for); boot
  recovery `await ledger.recover()` before adapters accept turns (table.rows_for_session_key to avoid
  regenerating); on (re)connect `await ledger.platform_reconnected(name)` + router.attach(name, sender);
  fatal disconnect router.detach(name); shutdown ledger.aclose(), router.close(); router =
  DeliveryRouter(scope, homes=…, directory=ChannelDirectory(scope), mirror=Mirror(store, service));
  directory.start(lambda: adapters, store) / directory.stop(task).
- Decisions (need D-ids): only turn finals ledgered (cron keeps its own); no "delivery failed" notice when
  the ledger will retry; labels only when a duplicate is possible; ledger stores masked text; _finish
  waits ≤300 s for turn-end before clearing the marker; mirror notes via the note channel; `indus send
  --file` attaches; owner liveness via token_locks helpers until P15-24's liveness module.
- Follow-ups: `indus send` connects adapters for one send — P15-33..35 need a send-only open (Telegram
  long polling must not compete with a running daemon); a sender vanishing between claim and redelivery
  spends one attempt; GatewaySection merge with other slices.
