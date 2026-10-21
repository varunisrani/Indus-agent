// Fake windows and screen for the ambient (P20-35, P20-36) main-process tests.

import type { Point } from "../electron/ambient/hud.ts";
import type { DisplayLike, Rect } from "../electron/windows/windowState.ts";

let nextId = 100;

export class FakeWindow {
  readonly webContents: {
    id: number;
    sent: Array<[string, unknown]>;
    on(): void;
    reload(): void;
    getURL(): string;
    getZoomLevel(): number;
    setZoomLevel(): void;
    isDestroyed(): boolean;
    send(channel: string, payload: unknown): void;
  };
  bounds: Rect;
  visible = false;
  minimized = false;
  destroyed = false;
  focused = 0;
  skipTaskbar: boolean | null = null;
  resizable = false;
  calls: string[] = [];
  private readonly listeners = new Map<string, Array<(...args: unknown[]) => void>>();

  constructor(bounds: Rect = { x: 0, y: 0, width: 800, height: 600 }) {
    this.bounds = { ...bounds };
    this.webContents = {
      id: nextId++,
      sent: [],
      on() {},
      reload() {},
      getURL: () => "file:///app/index.html",
      getZoomLevel: () => 0,
      setZoomLevel() {},
      isDestroyed: () => this.destroyed,
      send(channel, payload) {
        this.sent.push([channel, payload]);
      },
    };
  }

  on(event: string, listener: (...args: never[]) => void): this {
    const list = this.listeners.get(event) ?? [];
    list.push(listener as (...args: unknown[]) => void);
    this.listeners.set(event, list);
    return this;
  }

  emit(event: string, ...args: unknown[]): void {
    for (const listener of [...(this.listeners.get(event) ?? [])]) listener(...args);
  }

  /** Emit a preventable event; true when a listener prevented it. */
  emitPreventable(event: string): boolean {
    let prevented = false;
    this.emit(event, { preventDefault: () => (prevented = true) });
    return prevented;
  }

  loadURL(): Promise<void> {
    return Promise.resolve();
  }
  show(): void {
    this.calls.push("show");
    this.visible = true;
    this.minimized = false;
    this.emit("show");
  }
  showInactive(): void {
    this.calls.push("showInactive");
    this.visible = true;
    this.emit("show");
  }
  hide(): void {
    this.calls.push("hide");
    this.visible = false;
  }
  focus(): void {
    this.focused += 1;
    this.calls.push("focus");
  }
  blur(): void {
    this.calls.push("blur");
  }
  restore(): void {
    this.calls.push("restore");
    this.minimized = false;
    this.emit("restore");
  }
  minimize(): void {
    this.calls.push("minimize");
    this.minimized = true;
    this.emit("minimize");
  }
  maximize(): void {}
  unmaximize(): void {}
  close(): void {
    this.calls.push("close");
    if (this.emitPreventable("close")) return;
    this.destroy();
  }
  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.visible = false;
    this.emit("closed");
  }
  isDestroyed(): boolean {
    return this.destroyed;
  }
  isVisible(): boolean {
    return this.visible;
  }
  isMinimized(): boolean {
    return this.minimized;
  }
  isMaximized(): boolean {
    return false;
  }
  isFullScreen(): boolean {
    return false;
  }
  isFocused(): boolean {
    return false;
  }
  getNormalBounds(): Rect {
    return { ...this.bounds };
  }
  getBounds(): Rect {
    return { ...this.bounds };
  }
  setBounds(bounds: Rect): void {
    if (
      !this.resizable &&
      (bounds.width !== this.bounds.width || bounds.height !== this.bounds.height)
    )
      throw new Error("not resizable");
    this.bounds = { ...bounds };
    this.emit("move");
  }
  setResizable(on: boolean): void {
    this.resizable = on;
  }
  setSkipTaskbar(skip: boolean): void {
    this.skipTaskbar = skip;
  }
}

export class FakeScreen {
  cursor: Point = { x: 100, y: 100 };
  displays: DisplayLike[];

  constructor(displays: DisplayLike[] = [{ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }]) {
    this.displays = displays;
  }

  getAllDisplays(): DisplayLike[] {
    return this.displays;
  }
  getPrimaryDisplay(): DisplayLike {
    return this.displays[0] as DisplayLike;
  }
  getCursorScreenPoint(): Point {
    return { ...this.cursor };
  }
  getDisplayNearestPoint(point: Point): DisplayLike {
    return (
      this.displays.find(
        (d) =>
          point.x >= d.workArea.x &&
          point.x < d.workArea.x + d.workArea.width &&
          point.y >= d.workArea.y &&
          point.y < d.workArea.y + d.workArea.height,
      ) ?? (this.displays[0] as DisplayLike)
    );
  }
}
