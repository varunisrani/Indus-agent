// The shell's view of the dashboard plugins (P24-37): the manifests of the plugins that are on,
// whether the first render may finish (every plugin registered, or the wait ran out), and a
// version that changes whenever a plugin registers, so late registrations re-render the routes.

import { useEffect, useState, useSyncExternalStore } from "react";
import {
  fetchManifests,
  type LoadOptions,
  loadPlugins,
  type PluginManifest,
  type PluginRegistry,
  pluginRegistry,
} from "./registry.ts";

export interface PluginState {
  manifests: PluginManifest[];
  /** False until the plugins registered or the first-render wait ran out. */
  settled: boolean;
  version: number;
}

export function usePluginState(
  options: {
    registry?: PluginRegistry;
    fetch?: () => Promise<PluginManifest[]>;
    load?: (manifests: PluginManifest[], options: LoadOptions) => Promise<void>;
  } = {},
): PluginState {
  const registry = options.registry ?? pluginRegistry;
  const fetchList = options.fetch ?? fetchManifests;
  const load = options.load ?? loadPlugins;
  const [manifests, setManifests] = useState<PluginManifest[]>([]);
  const [settled, setSettled] = useState(false);
  const version = useSyncExternalStore(registry.subscribe, registry.snapshot, registry.snapshot);

  useEffect(() => {
    let live = true;
    setSettled(false);
    fetchList()
      .then(async (list) => {
        if (!live) return;
        setManifests(list);
        await load(list, { registry });
      })
      .catch(() => {
        // no plugin list (an older backend, or it failed): the shell runs without plugins
      })
      .finally(() => {
        if (live) setSettled(true);
      });
    return () => {
      live = false;
    };
  }, [fetchList, load, registry]);

  return { manifests, settled, version };
}
