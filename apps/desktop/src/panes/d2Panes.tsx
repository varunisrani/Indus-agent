// The working panes (P20-28 preview, P20-29 files, P20-30 git review, P20-31 terminal), registered
// with the pane shell's registry (P20-27). Each definition turns the shell's render context into the
// pane's props; the shell decides where and when they show. `registerWorkingPanes()` runs once at
// start (`main.tsx`).

import { useEffect, useState, useSyncExternalStore } from "react";
import { desktopBridge } from "../app/bridge.ts";
import type { IndusDesktopBridge } from "../global.d.ts";
import { useT } from "../i18n/index.tsx";
import { FilesPane } from "./files/FilesPane.tsx";
import { type PaneDefinition, type PaneRenderContext, paneRegistry } from "./PaneShell.tsx";
import { PreviewPane } from "./preview/PreviewPane.tsx";
import type { PreviewTarget } from "./preview/target.ts";
import { draftWithAgent, trackTurnBase } from "./review/agentDraft.ts";
import { ReviewPane } from "./review/ReviewPane.tsx";
import { TerminalPane } from "./terminal/TerminalPane.tsx";

/** What the preview pane shows when the shell has no target store of its own. */
class PreviewTargets {
  private target: PreviewTarget | null = null;
  private readonly listeners = new Set<() => void>();
  get = (): PreviewTarget | null => this.target;
  set = (target: PreviewTarget | null): void => {
    this.target = target;
    for (const listener of [...this.listeners]) listener();
  };
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
}

/** The window's preview target: the shell's `openPreview` sets it and opens the pane. */
export const previewTargets = new PreviewTargets();

function openPreview(context: PaneRenderContext, target: PreviewTarget): void {
  if (context.openPreview !== undefined) context.openPreview(target);
  else previewTargets.set(target);
}

function PreviewHost(props: { context: PaneRenderContext }) {
  const target = useSyncExternalStore(
    previewTargets.subscribe,
    previewTargets.get,
    previewTargets.get,
  );
  const { context } = props;
  return (
    <PreviewPane
      target={target}
      mode={context.mode}
      hidden={context.hidden}
      onNavigate={previewTargets.set}
      sessionId={context.sessionId}
      {...(context.addToChat === undefined
        ? {}
        : { onAnnotationSend: (text: string) => context.addToChat?.(text) })}
    />
  );
}

function ReviewHost(props: { context: PaneRenderContext }) {
  const t = useT();
  const { context } = props;
  const [base, setBase] = useState<string | null>(null);
  const { connection, sessionId, workspace } = context;

  useEffect(() => {
    setBase(null);
    const git = (desktopBridge() as IndusDesktopBridge | null)?.git;
    if (connection === null || sessionId === null || workspace === null || git === undefined)
      return;
    return trackTurnBase(
      connection,
      sessionId,
      async () => (await git.status({ repo: workspace })).head,
      setBase,
    );
  }, [connection, sessionId, workspace]);

  if (context.mode === "remote")
    return <p className="p-3 text-sm text-muted">{t("pane.unavailable")}</p>;
  return (
    <ReviewPane
      repo={workspace}
      hidden={context.hidden}
      lastTurnBase={base}
      {...(connection === null || sessionId === null
        ? {}
        : { draftMessage: (prompt: string) => draftWithAgent(connection, sessionId, prompt) })}
      {...(context.addToChat === undefined
        ? {}
        : { askAgent: (prompt: string) => context.addToChat?.(prompt) })}
    />
  );
}

export const WORKING_PANES: readonly PaneDefinition[] = [
  {
    id: "preview",
    stateful: true,
    render: (context) => <PreviewHost context={context} />,
  },
  {
    id: "files",
    stateful: false,
    render: (context) => (
      <FilesPane
        workspace={context.workspace}
        mode={context.mode}
        hidden={context.hidden}
        onPreview={(path) => openPreview(context, { kind: "file", path })}
      />
    ),
  },
  {
    id: "review",
    stateful: false,
    render: (context) => <ReviewHost context={context} />,
  },
  {
    id: "terminal",
    stateful: true,
    render: (context) => (
      <TerminalPane
        cwd={context.workspace}
        mode={context.mode}
        hidden={context.hidden}
        {...(context.addToChat === undefined
          ? {}
          : { onAddToChat: (text: string) => context.addToChat?.(text) })}
      />
    ),
  },
];

let registered = false;

/** Register the four panes with the window's pane registry (once). */
export function registerWorkingPanes(): void {
  if (registered) return;
  registered = true;
  for (const definition of WORKING_PANES) paneRegistry.register(definition);
}
