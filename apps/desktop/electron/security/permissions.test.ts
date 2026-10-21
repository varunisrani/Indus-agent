import { describe, expect, it, vi } from "vitest";
import { fileAppOrigin } from "../appOrigin.ts";
import {
  installDenyAllPermissions,
  installPermissionPolicy,
  type PermissionSession,
  permissionAllowed,
} from "./permissions.ts";

const ORIGIN = fileAppOrigin("/app/dist/index.html", "linux");
const APP = "file:///app/dist/index.html#/";

describe("permission policy", () => {
  it.each(["clipboard-sanitized-write", "notifications", "fullscreen"])(
    "app may use %s",
    (permission) => {
      expect(permissionAllowed(permission, APP, {}, ORIGIN)).toBe(true);
    },
  );

  it("media: audio only", () => {
    expect(permissionAllowed("media", APP, { mediaTypes: ["audio"] }, ORIGIN)).toBe(true);
    expect(permissionAllowed("media", APP, { mediaTypes: ["audio", "video"] }, ORIGIN)).toBe(false);
    expect(permissionAllowed("media", APP, { mediaTypes: [] }, ORIGIN)).toBe(false);
  });

  it.each([
    "geolocation",
    "openExternal",
    "midi",
    "pointerLock",
    "display-capture",
    "clipboard-read",
  ])("%s is denied", (permission) => {
    expect(permissionAllowed(permission, APP, {}, ORIGIN)).toBe(false);
  });

  it("other origins and subframes get nothing", () => {
    expect(permissionAllowed("notifications", "https://evil.example/", {}, ORIGIN)).toBe(false);
    expect(permissionAllowed("notifications", APP, { isMainFrame: false }, ORIGIN)).toBe(false);
  });

  it("installs request and check handlers that apply the policy", () => {
    const installed: {
      request?: Parameters<PermissionSession["setPermissionRequestHandler"]>[0];
      check?: Parameters<PermissionSession["setPermissionCheckHandler"]>[0];
    } = {};
    const session: PermissionSession = {
      setPermissionRequestHandler: (h) => {
        installed.request = h;
      },
      setPermissionCheckHandler: (h) => {
        installed.check = h;
      },
    };
    installPermissionPolicy(session, { appOrigin: ORIGIN, log: () => {} });
    const callback = vi.fn();
    installed.request?.({}, "geolocation", callback, { requestingUrl: APP });
    installed.request?.({}, "notifications", callback, { requestingUrl: APP });
    expect(callback.mock.calls).toEqual([[false], [true]]);
    expect(installed.check?.({}, "notifications", "file://", { requestingUrl: APP })).toBe(true);
    expect(installed.check?.({}, "notifications", "https://evil.example", {})).toBe(false);
  });

  it("deny-all for untrusted partitions", () => {
    const callback = vi.fn();
    const installed: { check?: () => boolean } = {};
    installDenyAllPermissions({
      setPermissionRequestHandler: (h) => h({}, "notifications", callback, {}),
      setPermissionCheckHandler: (h) => {
        installed.check = h as () => boolean;
      },
    });
    expect(callback).toHaveBeenCalledWith(false);
    expect(installed.check?.()).toBe(false);
  });
});
