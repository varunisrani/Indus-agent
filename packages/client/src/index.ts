// @indus/client: the one connection layer shared by the TUI, web and desktop surfaces.
// `stdioTransport` is Node-only and has its own entry so browser bundles never pull it in.

export {
  type BackoffOptions,
  backoffDelay,
  type ReconnectingEvent,
  Reconnector,
} from "./backoff.ts";
export {
  type CallOptions,
  type ChannelOptions,
  type CloseInfo,
  DECLINE_CAPABILITY,
  declineAnswer,
  type HandlerTable,
  type IncomingRequest,
  type OutboundFrame,
  type RequestId,
  RpcChannel,
  type RpcRequestHandler,
  type ServerRequestHandlers,
  type Transport,
  type Unsubscribe,
} from "./channel.ts";
export {
  type ClientReplayOptions,
  type ConnectHooks,
  IndusClient,
  type IndusClientOptions,
} from "./client.ts";
export { type Clock, systemClock } from "./clock.ts";
export {
  errorNameFor,
  GatewayRpcError,
  isKnownErrorCode,
  NotConnected,
  RequestAborted,
  RpcTimeout,
  rpcErrorFromWire,
  TransportClosed,
  TransportReplaced,
} from "./errors.ts";
export { EventBuffer, type EventListener } from "./eventBuffer.ts";
export { Heartbeat, type HeartbeatOptions } from "./heartbeat.ts";
export * from "./limits.ts";
export * from "./localEvents.ts";
export { cutLine, LogRing, redactUrls } from "./logRing.ts";
export { encodeFrame, LineSplitter, type ParsedLine, parseFrameLine, preview } from "./ndjson.ts";
export {
  type Admission,
  type Cursor,
  REPLAY_CURSOR_CAPACITY,
  REPLAY_GAP_EVENT,
  ReplayCursors,
  type ReplayOptions,
} from "./replay.ts";
export { Ring } from "./ring.ts";
export {
  attachUrlProblem,
  CLOSE_REASONS,
  closeReason,
  GATEWAY_SUBPROTOCOL,
  protocolsProblem,
  TERMINAL_CLOSE_CODES,
  TICKET_SUBPROTOCOL_PREFIX,
  type WebSocketFactory,
  type WebSocketLike,
  WS_OPEN_TIMEOUT_MS,
  WsTransport,
  type WsTransportOptions,
} from "./ws-transport.ts";
