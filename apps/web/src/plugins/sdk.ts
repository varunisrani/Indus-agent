// The object dashboard plugins get on `window.__INDUS_DASHBOARD_SDK__` (P24-37). Plugin bundles
// are plain scripts that do not bundle React: they take it, the hooks and our UI primitives from
// here, and reach the backend only through the helpers below:
//
// - `api` / `fetchJSON(url, init)`: the dashboard's REST client (session token header in loopback
//   mode, the cookie in gated mode; profile scope added like every page's call);
// - `authedFetch(url, init)`: the same, returning the raw response (uploads, downloads);
// - `pluginFetch(name, path, init)`: JSON from the plugin's own backend under `/api/ext/<name>/`;
// - `openSocket(name, path, params)`: a WebSocket to `/api/ext/<name><path>` through the shared
//   socket helper — a fresh single-use ticket in gated mode, the session token in loopback mode,
//   always as a subprotocol and never in the URL (R09.2).
//
// Plugins are not sandboxed (R07.19): this object is a convenience, not a boundary. Enabling a
// plugin is what grants it the page (P13 consent).

import * as React from "react";
import { type ApiInit, api, rawRequest, request } from "../lib/api.ts";
import { formatBytes, formatCount, relativeTime } from "../lib/format.ts";
import { openDashboardSocket } from "../lib/wsUrl.ts";
import { useToast } from "../shell/Toasts.tsx";
import { Badge, Button, Card, Field, Notice } from "../ui/controls.tsx";
import { ConfirmDialog, Modal } from "../ui/Dialog.tsx";
import { Switch } from "../ui/Switch.tsx";
import { pluginRegistry } from "./registry.ts";
import { KNOWN_SLOTS, PluginSlot } from "./slots.tsx";

/** Bumped on a change that breaks plugins; additions keep it. */
export const SDK_VERSION = "1.0.0";
/** The WebSocket purpose plugin sockets are minted and checked for. */
export const PLUGIN_SOCKET_PURPOSE = "web-plugin";
const EXT_PREFIX = "/api/ext";

function extPath(name: string, path: string): string {
  const tail = path.startsWith("/") ? path : `/${path}`;
  return `${EXT_PREFIX}/${encodeURIComponent(name)}${tail}`;
}

export function buildDashboardSdk() {
  return Object.freeze({
    version: SDK_VERSION,
    React,
    hooks: {
      useState: React.useState,
      useEffect: React.useEffect,
      useCallback: React.useCallback,
      useMemo: React.useMemo,
      useRef: React.useRef,
      useContext: React.useContext,
      createContext: React.createContext,
      useToast,
    },
    api,
    fetchJSON: <T>(url: string, init?: ApiInit) => request<T>(url, init),
    authedFetch: (url: string, init?: ApiInit) => rawRequest(url, init),
    pluginFetch: <T>(name: string, path: string, init?: ApiInit) =>
      request<T>(extPath(name, path), init),
    openSocket: (name: string, path: string, params: Record<string, string | undefined> = {}) =>
      openDashboardSocket(extPath(name, path), PLUGIN_SOCKET_PURPOSE, params),
    components: {
      Badge,
      Button,
      Card,
      ConfirmDialog,
      Field,
      Modal,
      Notice,
      PluginSlot,
      Switch,
    },
    slots: KNOWN_SLOTS,
    utils: { formatBytes, formatCount, relativeTime },
    registry: { slotNames: () => KNOWN_SLOTS, version: () => pluginRegistry.snapshot() },
  });
}

export type DashboardSdk = ReturnType<typeof buildDashboardSdk>;
