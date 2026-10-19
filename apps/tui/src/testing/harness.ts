// Test wiring: a fixture's mock gateway, an `IndusClient` connected to it, and the renderer's event
// handler and turn controller on top, with fresh stores.

import { type ClientEvent, type HandlerTable, IndusClient } from "@indus/client";
import { type Rpc, rpcFromClient } from "../app/rpc.ts";
import { createEventHandler } from "../events/handleEvent.ts";
import { focusSession } from "../state/destination.ts";
import { resetOverlay } from "../state/overlayStore.ts";
import { transcriptStore } from "../state/transcriptStore.ts";
import { turnStore } from "../state/turnStore.ts";
import { resetUi } from "../state/uiStore.ts";
import { TurnController } from "../turn/controller.ts";
import { type MockGateway, type MockGatewayOptions, mockGateway } from "./mockGateway.ts";

export function resetStores(): void {
  resetUi();
  resetOverlay();
  turnStore.getState().reset();
  transcriptStore.getState().clear();
}

export interface Rig {
  gateway: MockGateway;
  client: IndusClient;
  rpc: Rpc;
  controller: TurnController;
  /** Every event the client delivered, in order. */
  events: ClientEvent[];
}

export interface RigOptions extends MockGatewayOptions {
  handlers?: HandlerTable;
  /** Focus this session before the replay (the session filter needs a focused id). */
  focus?: string;
}

/** Fresh stores, a gateway for `fixture`, a started client, and the handler subscribed. */
export function rig(fixture: string, options: RigOptions = {}): Rig {
  resetStores();
  if (options.focus !== undefined) focusSession(options.focus);
  const gateway = mockGateway(fixture, options);
  const client = new IndusClient({ connect: gateway.server.connect });
  const rpc = rpcFromClient(client);
  const controller = new TurnController({ rpc });
  const handle = createEventHandler({ controller });
  const events: ClientEvent[] = [];
  client.subscribe((event) => {
    events.push(event);
    handle(event);
  });
  if (options.handlers !== undefined) client.handle(options.handlers);
  client.start();
  return { gateway, client, rpc, controller, events };
}
