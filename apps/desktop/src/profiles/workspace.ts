// A profile's chat world in this window (P20-26): its connection (from the window's one
// ConnectionStore), its transcripts and the chat slots bound to both. A soft re-home wipes it:
// the transcripts are thrown away and rebuilt on the same connection, open server requests are
// cleared, and listeners re-render with the new stores.

import type { AppSlots } from "../app/App.tsx";
import { desktopBridge } from "../app/bridge.ts";
import { restHistory } from "../chat/restHistory.ts";
import { TranscriptStore, type TranscriptStoreOptions } from "../chat/transcriptStore.ts";
import type { ConnectionStore, GatewayConnection } from "../gateway/connectionStore.ts";
import type { ProfileWorkspace } from "./switching.ts";

export type SlotFactory = (workspace: {
  profile: string;
  connection: GatewayConnection;
  transcripts: TranscriptStore;
}) => AppSlots;

export class ChatWorkspace implements ProfileWorkspace {
  readonly profile: string;
  readonly connection: GatewayConnection;
  private readonly connections: ConnectionStore;
  private readonly makeSlots: SlotFactory;
  private readonly listeners = new Set<() => void>();
  private store: TranscriptStore;
  private slotsValue: AppSlots;
  private version = 0;

  constructor(profile: string, connections: ConnectionStore, makeSlots: SlotFactory) {
    this.profile = profile;
    this.connections = connections;
    this.connection = connections.connection(profile);
    this.makeSlots = makeSlots;
    this.store = new TranscriptStore(this.connection, this.storeOptions());
    this.slotsValue = makeSlots({ profile, connection: this.connection, transcripts: this.store });
  }

  /** Stored rows come over main's REST proxy, for this profile (the daemon WS serves events). */
  private storeOptions(): TranscriptStoreOptions {
    const api = desktopBridge()?.api;
    return api === undefined
      ? {}
      : { history: restHistory((request) => api.request(request), this.profile) };
  }

  get transcripts(): TranscriptStore {
    return this.store;
  }

  get slots(): AppSlots {
    return this.slotsValue;
  }

  /** Changes on every wipe (React keys and `useSyncExternalStore`). */
  getVersion(): number {
    return this.version;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  wipe(): void {
    this.store.dispose();
    this.connection.serverRequests.clear();
    this.store = new TranscriptStore(this.connection, this.storeOptions());
    this.slotsValue = this.makeSlots({
      profile: this.profile,
      connection: this.connection,
      transcripts: this.store,
    });
    this.version += 1;
    for (const listener of [...this.listeners]) listener();
  }

  async dispose(): Promise<void> {
    this.store.dispose();
    await this.connections.release(this.profile);
  }
}
