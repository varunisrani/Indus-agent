// Which conversation the person used last (P20-35). App windows report the session their route
// shows (`indus:session:active`) whenever it changes and when they gain focus; the newest report
// from a window that still exists wins. Quick entry delivers into it and the HUD toggle opens it.

export interface ActiveReport {
  contentsId: number;
  sessionId: string | null;
  at: number;
}

export class ActiveSessions {
  private readonly reports = new Map<number, ActiveReport>();
  private serial = 0;

  /** A window showed `sessionId` (null: a new chat) — now. */
  report(contentsId: number, sessionId: string | null): void {
    this.serial += 1;
    this.reports.set(contentsId, { contentsId, sessionId, at: this.serial });
  }

  /** A window gained focus: it becomes the latest with what it showed last. */
  touch(contentsId: number): void {
    const known = this.reports.get(contentsId);
    this.report(contentsId, known?.sessionId ?? null);
  }

  forget(contentsId: number): void {
    this.reports.delete(contentsId);
  }

  /** The newest report from a window `isLive` still accepts, or null. */
  latest(isLive: (contentsId: number) => boolean): ActiveReport | null {
    let best: ActiveReport | null = null;
    for (const report of this.reports.values()) {
      if (!isLive(report.contentsId)) continue;
      if (best === null || report.at > best.at) best = report;
    }
    return best === null ? null : { ...best };
  }
}
