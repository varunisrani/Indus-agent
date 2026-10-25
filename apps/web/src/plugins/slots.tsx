// Plugin slots (P24-37): named places in the shell and in pages that plugins can fill with
// `registerSlot(name, slot, Component)`. Several plugins may fill one slot; they render in the
// order they registered, each inside its own error boundary so a broken plugin hides itself
// instead of the page. A slot re-renders when a plugin registers late.

import { Component, type ComponentType, type ReactNode, useSyncExternalStore } from "react";
import { type PluginManifest, type PluginRegistry, pluginRegistry } from "./registry.ts";

/** Shell slots, by name (alphabetical). */
export const SHELL_SLOTS = [
  "backdrop",
  "footer-left",
  "footer-right",
  "header-banner",
  "header-left",
  "header-right",
  "overlay",
  "post-main",
  "pre-main",
  "sidebar",
] as const;

export const SLOT_PAGES = [
  "sessions",
  "analytics",
  "logs",
  "cron",
  "skills",
  "plugins",
  "config",
  "env",
  "chat",
  "docs",
] as const;

/** Every slot name the shell and pages render. */
export const KNOWN_SLOTS: readonly string[] = [
  ...SHELL_SLOTS,
  ...SLOT_PAGES.flatMap((page) => [`${page}:top`, `${page}:bottom`]),
];

class SlotBoundary extends Component<{ plugin: string; children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: unknown): void {
    // biome-ignore lint/suspicious/noConsole: a plugin's render failure only warns
    console.warn(`[plugins] ${this.props.plugin} failed to render`, error);
  }

  override render(): ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}

export function PluginSlot({
  name,
  fallback = null,
  registry = pluginRegistry,
}: {
  name: string;
  fallback?: ReactNode;
  registry?: PluginRegistry;
}) {
  useSyncExternalStore(registry.subscribe, registry.snapshot, registry.snapshot);
  const entries = registry.slotEntries(name);
  if (entries.length === 0) return <>{fallback}</>;
  return (
    <>
      {entries.map(({ plugin, component: Slot }) => (
        <SlotBoundary key={plugin} plugin={plugin}>
          <Slot />
        </SlotBoundary>
      ))}
    </>
  );
}

/** A plugin's page (the route element), with the plugin's own boundary. */
export function PluginPage({
  manifest,
  registry = pluginRegistry,
}: {
  manifest: PluginManifest;
  registry?: PluginRegistry;
}) {
  useSyncExternalStore(registry.subscribe, registry.snapshot, registry.snapshot);
  const Page = registry.component(manifest.name);
  if (Page === undefined) {
    const error = registry.loadError(manifest.name);
    return (
      <p className="p-6 text-sm text-muted">
        {error === "LOAD_FAILED"
          ? `The ${manifest.label} plugin could not be loaded.`
          : `The ${manifest.label} plugin has not registered its page yet.`}
      </p>
    );
  }
  return (
    <SlotBoundary plugin={manifest.name}>
      <Page />
    </SlotBoundary>
  );
}

const pages = new Map<string, { manifest: PluginManifest; page: ComponentType }>();

/** One stable route component per plugin (so a re-render never remounts the plugin's page). */
export function pluginPageFor(manifest: PluginManifest): ComponentType {
  const known = pages.get(manifest.name);
  if (known !== undefined && known.manifest === manifest) return known.page;
  const page = () => <PluginPage manifest={manifest} />;
  pages.set(manifest.name, { manifest, page });
  return page;
}
