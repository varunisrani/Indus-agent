// What main's install steps share. `main.ts` builds one `MainContext` on ready and passes it to
// each `install…(context)` call in its explicit list; later tasks add their own install step and
// read what they need from here (P20-08+ add fields as they land).

import type { AppOrigin } from "./appOrigin.ts";
import type { IpcRegistry } from "./ipc/registry.ts";
import type { DesktopLog } from "./log.ts";
import type { DesktopPaths } from "./paths.ts";
import type { DesktopSecrets } from "./secrets.ts";
import type { CspState } from "./security/csp.ts";
import type { ExternalOpener, UserActivationTracker } from "./security/externalOpen.ts";
import type { ShellPathOutcome } from "./shellPath.ts";
import type { MachineryGate } from "./singleInstance.ts";
import type { WindowManager } from "./windows/manager.ts";

export interface MainContext {
  readonly registry: IpcRegistry;
  readonly windows: WindowManager;
  readonly log: DesktopLog;
  readonly paths: DesktopPaths;
  readonly appOrigin: AppOrigin;
  /** Add backend origins to `connect-src` as they become known (P20-10). */
  readonly csp: CspState;
  readonly dev: boolean;
  readonly packaged: boolean;
  readonly platform: string;
  readonly appVersion: string;
  readonly deepLinkScheme: string;
  /** Wrap every spawn / reap / claim of a backend with `machinery.guard` (R08.3). */
  readonly machinery: MachineryGate;
  readonly activation: UserActivationTracker;
  readonly externalOpener: ExternalOpener;
  readonly secrets: DesktopSecrets;
  /** Settles once the login-shell PATH merge finished (≤ 3 s); await before spawning (R08.16). */
  readonly shellPath: Promise<ShellPathOutcome>;
}
