// Notifications (P20-36): `indus:notify` from a window → an OS notification. The same tag replaces
// the previous one. A click raises the window that asked (a user action, the only kind that may
// focus) and tells it which notification and which action (`indus:notification:action`).
//
// Actions: macOS shows them natively; Linux sends them over D-Bus to the desktop's notification
// service when it advertises `actions` and follows its answers; elsewhere a click only. A D-Bus
// failure falls back to a plain notification. Our own completion sound plays in the window, so
// every OS notification is silent.

import type { GdbusClient, LineStream, NotificationSignal } from "./dbus.ts";

export interface NotificationRequest {
  title: string;
  body?: string | undefined;
  tag?: string | undefined;
  actions?: ReadonlyArray<{ id: string; label: string }> | undefined;
}

export interface ElectronNotificationLike {
  show(): void;
  close(): void;
  on(event: "click", listener: () => void): unknown;
  on(event: "action", listener: (event: unknown, index: number) => void): unknown;
  on(event: "close" | "failed", listener: () => void): unknown;
}

export interface ElectronNotificationOptions {
  title: string;
  body: string;
  silent: true;
  actions?: Array<{ type: "button"; text: string }>;
}

export interface NotificationDeps {
  platform: string;
  appName: string;
  isSupported(): boolean;
  create(options: ElectronNotificationOptions): ElectronNotificationLike;
  /** Linux: the D-Bus client, or null to use Electron's notifications only. */
  dbus: Pick<
    GdbusClient,
    "notificationCapabilities" | "notify" | "close" | "watchNotifications"
  > | null;
  /** Notifications are on (settings). */
  enabled(): boolean;
  /** The person clicked notification `tag` (action `actionId`, null for the body) from `senderId`. */
  activate(senderId: number, tag: string | null, actionId: string | null): void;
  log(line: string): void;
}

interface Shown {
  close(): void;
  dbusId?: number;
}

interface DbusEntry {
  senderId: number;
  tag: string | null;
  actionIds: ReadonlySet<string>;
}

export class NotificationCenter {
  private readonly deps: NotificationDeps;
  private readonly byTag = new Map<string, Shown>();
  private readonly dbusLive = new Map<number, DbusEntry>();
  private capabilities: Promise<string[] | null> | null = null;
  private monitor: LineStream | null = null;

  constructor(deps: NotificationDeps) {
    this.deps = deps;
  }

  async show(senderId: number, request: NotificationRequest): Promise<{ shown: boolean }> {
    if (!this.deps.enabled()) return { shown: false };
    const tag = request.tag ?? null;
    const actions = request.actions ?? [];
    const previous = tag === null ? undefined : this.byTag.get(tag);

    if (this.deps.platform === "linux" && this.deps.dbus !== null) {
      const viaDbus = await this.showOverDbus(senderId, request, tag, previous?.dbusId);
      if (viaDbus !== null) {
        if (tag !== null) this.byTag.set(tag, viaDbus);
        return { shown: true };
      }
    }

    if (!this.deps.isSupported()) return { shown: false };
    previous?.close();
    let notification: ElectronNotificationLike;
    try {
      notification = this.deps.create({
        title: request.title,
        body: request.body ?? "",
        silent: true,
        ...(this.deps.platform === "darwin" && actions.length > 0
          ? { actions: actions.map((action) => ({ type: "button" as const, text: action.label })) }
          : {}),
      });
    } catch {
      this.deps.log("[notify] the notification could not be created");
      return { shown: false };
    }
    notification.on("click", () => this.deps.activate(senderId, tag, null));
    notification.on("action", (_event, index) => {
      const action = Number.isInteger(index) ? actions[index] : undefined;
      if (action !== undefined) this.deps.activate(senderId, tag, action.id);
    });
    const shown: Shown = { close: () => notification.close() };
    const forget = () => {
      if (tag !== null && this.byTag.get(tag) === shown) this.byTag.delete(tag);
    };
    notification.on("close", forget);
    notification.on("failed", forget);
    if (tag !== null) this.byTag.set(tag, shown);
    try {
      notification.show();
    } catch {
      forget();
      return { shown: false };
    }
    return { shown: true };
  }

  dispose(): void {
    this.monitor?.stop();
    this.monitor = null;
    for (const shown of this.byTag.values()) shown.close();
    this.byTag.clear();
    this.dbusLive.clear();
  }

  // -- Linux over D-Bus --------------------------------------------------------------------------

  private async showOverDbus(
    senderId: number,
    request: NotificationRequest,
    tag: string | null,
    replacesId: number | undefined,
  ): Promise<Shown | null> {
    const dbus = this.deps.dbus;
    if (dbus === null) return null;
    if (this.capabilities === null) this.capabilities = dbus.notificationCapabilities();
    const caps = await this.capabilities;
    if (caps === null) {
      this.capabilities = null; // ask again next time (the service may start later)
      return null;
    }
    const withActions = caps.includes("actions");
    const actions = request.actions ?? [];
    const pairs: Array<readonly [string, string]> = withActions
      ? [["default", "Open"], ...actions.map((a) => [a.id, a.label] as const)]
      : [];
    if (withActions) this.ensureMonitor();
    const id = await dbus.notify({
      appName: this.deps.appName,
      title: request.title,
      body: request.body ?? "",
      actions: pairs,
      ...(replacesId === undefined ? {} : { replacesId }),
    });
    if (id === null) {
      this.deps.log("[notify] the D-Bus notification service did not take it; using a plain one");
      return null;
    }
    if (replacesId !== undefined && replacesId !== id) this.dbusLive.delete(replacesId);
    this.dbusLive.set(id, {
      senderId,
      tag,
      actionIds: new Set(actions.map((action) => action.id)),
    });
    return {
      dbusId: id,
      close: () => {
        this.dbusLive.delete(id);
        void dbus.close(id).catch(() => {});
      },
    };
  }

  private ensureMonitor(): void {
    const dbus = this.deps.dbus;
    if (this.monitor !== null || dbus === null) return;
    this.monitor = dbus.watchNotifications(
      (signal) => this.onSignal(signal),
      () => {
        this.monitor = null;
      },
    );
  }

  private onSignal(signal: NotificationSignal): void {
    const entry = this.dbusLive.get(signal.id);
    if (entry === undefined) return;
    if (signal.kind === "closed") {
      this.dbusLive.delete(signal.id);
      if (entry.tag !== null && this.byTag.get(entry.tag)?.dbusId === signal.id)
        this.byTag.delete(entry.tag);
      return;
    }
    if (signal.action === "default") this.deps.activate(entry.senderId, entry.tag, null);
    else if (entry.actionIds.has(signal.action))
      this.deps.activate(entry.senderId, entry.tag, signal.action);
  }
}
