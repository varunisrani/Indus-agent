// Shapes, helpers and the list loader the Capabilities tabs share (P24-26, W52-k).

import { useCallback, useEffect, useState } from "react";
import { type RestLike, rest } from "../../settings/settingsApi.ts";

// -- shapes (the REST answers, P18-14) ------------------------------------------------------------

export interface SkillRow {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
  source: string;
}

export interface HubResult {
  identifier: string;
  name: string;
  description: string;
  source: string;
  installed: boolean;
}

export interface ToolsetRow {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  tools: string[];
}

export interface McpServerRow {
  name: string;
  enabled?: boolean;
  kind?: string;
  command?: string | null;
  url?: string | null;
  auth?: string | null;
}

export interface AgentPluginRow {
  name: string;
  version: string | null;
  description: string;
  enabled: boolean;
  sha: string | null;
  /** Where it came from (`bundled`, `user`, a git address …). */
  source?: string;
}

export interface DesktopPluginRow {
  id: string;
  name: string;
  version?: string;
  enabled: boolean;
}

/** The app-level desktop plugins (U3's P24-32 contribution registry implements it). */
export interface DesktopPluginSource {
  list(): Promise<DesktopPluginRow[]>;
  /** Turn one desktop plugin on or off (absent: the Desktop switch is shown read-only). */
  setEnabled?(id: string, on: boolean): Promise<void>;
}

export type PluginBadge = "agent" | "desktop" | "both";

export interface PluginRowView {
  readonly name: string;
  readonly agent: AgentPluginRow | null;
  readonly desktop: DesktopPluginRow | null;
  readonly badge: PluginBadge;
}

/** One row per plugin name, with its Agent and Desktop halves. */
export function mergePlugins(
  agent: readonly AgentPluginRow[],
  desktop: readonly DesktopPluginRow[],
): PluginRowView[] {
  const names = new Map<
    string,
    { agent: AgentPluginRow | null; desktop: DesktopPluginRow | null }
  >();
  for (const row of agent) names.set(row.name, { agent: row, desktop: null });
  for (const row of desktop) {
    const key = row.name;
    const found = names.get(key);
    names.set(key, { agent: found?.agent ?? null, desktop: row });
  }
  return [...names.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, halves]) => ({
      name,
      ...halves,
      badge:
        halves.agent !== null && halves.desktop !== null
          ? "both"
          : halves.agent !== null
            ? "agent"
            : "desktop",
    }));
}

export const FULL_SHA = /^[0-9a-f]{40}$/i;

/** A list loaded for one profile; `reload` re-reads it. */
export function useList<T>(
  api: RestLike | null,
  path: string,
  profile: string,
  pick: (body: unknown) => T[],
): {
  rows: T[] | null;
  error: string | null;
  reload: () => void;
  /** Repaint the list at once (an optimistic toggle); the next reload replaces it. */
  setRows: (update: (rows: T[]) => T[]) => void;
} {
  const [rows, setRows] = useState<T[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [serial, setSerial] = useState(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: serial forces a reload
  useEffect(() => {
    let live = true;
    void rest<unknown>(api, "GET", path, { profile }).then((answer) => {
      if (!live) return;
      if (answer.ok) {
        setRows(pick(answer.data));
        setError(null);
      } else {
        setError(answer.message);
      }
    });
    return () => {
      live = false;
    };
  }, [api, path, profile, serial]);
  const patch = useCallback(
    (update: (rows: T[]) => T[]) =>
      setRows((current) => (current === null ? current : update(current))),
    [],
  );
  return { rows, error, reload: useCallback(() => setSerial((n) => n + 1), []), setRows: patch };
}

export function field<T>(key: string): (body: unknown) => T[] {
  return (body) => {
    const value = (body as Record<string, unknown> | null)?.[key];
    return Array.isArray(value) ? (value as T[]) : [];
  };
}
