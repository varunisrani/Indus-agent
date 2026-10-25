// The Toolsets view of Skills (W52-c): one card per toolset with its tools as chips and a Configure
// panel. Toolsets have no settings route yet, so the panel only points at the Keys page.

import { useQuery } from "@tanstack/react-query";
import {
  AppWindow,
  Brain,
  Code,
  Eye,
  FileText,
  Globe,
  Image,
  type LucideIcon,
  SquareTerminal,
  Wrench,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { api } from "../../lib/api.ts";
import type { ToolsetInfo } from "../../lib/restTypes.ts";
import { Badge, Button, QueryState } from "../../ui/controls.tsx";
import { DialogActions, Modal } from "../../ui/Dialog.tsx";

const ICONS: ReadonlyArray<[RegExp, LucideIcon]> = [
  [/web|search/, Globe],
  [/vision|eye/, Eye],
  [/browser/, AppWindow],
  [/terminal|shell/, SquareTerminal],
  [/code|exec/, Code],
  [/file/, FileText],
  [/image|media/, Image],
  [/memory/, Brain],
];

function iconFor(name: string): LucideIcon {
  return ICONS.find(([pattern]) => pattern.test(name.toLowerCase()))?.[1] ?? Wrench;
}

function matches(toolset: ToolsetInfo, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [toolset.name, toolset.label, toolset.description, ...toolset.tools].some((t) =>
    t.toLowerCase().includes(q),
  );
}

function ToolsetDrawer({ toolset, onClose }: { toolset: ToolsetInfo; onClose: () => void }) {
  return (
    <Modal
      open
      size="lg"
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={toolset.label || toolset.name}
      description={toolset.description || `Tools in ${toolset.name}.`}
    >
      <div className="flex flex-col gap-3 py-1">
        <div>
          <Badge tone={toolset.enabled ? "ok" : "neutral"}>
            {toolset.enabled ? "Active" : "Inactive"}
          </Badge>
        </div>
        <p className="m-0 text-[0.8125rem] text-ink-muted">
          Provider settings for toolsets are not editable here yet. Keys the tools need are kept on
          the Keys page.
        </p>
        <DialogActions>
          <Link
            to="/env"
            className="type-button inline-flex h-7 items-center border border-ink px-2.5 text-ink hover:bg-hover"
          >
            Open Keys
          </Link>
        </DialogActions>
      </div>
    </Modal>
  );
}

export function ToolsetsView({ query }: { query: string }) {
  const [open, setOpen] = useState<ToolsetInfo | null>(null);
  const toolsets = useQuery({
    queryKey: ["skills", "toolsets"],
    queryFn: () => api.skills.toolsets(),
  });
  const list = (toolsets.data?.toolsets ?? []).filter((t) => matches(t, query));
  return (
    <QueryState
      loading={toolsets.isPending}
      error={toolsets.error}
      empty={list.length === 0}
      emptyText={query ? "No toolsets match." : "No toolsets reported."}
    >
      <ul
        aria-label="Toolsets"
        className="m-0 grid list-none grid-cols-1 gap-3 p-0 md:grid-cols-2 xl:grid-cols-3"
      >
        {list.map((toolset) => {
          const Icon = iconFor(toolset.name);
          return (
            <li key={toolset.name} className="flex flex-col gap-2.5 border border-line p-4">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <Icon aria-hidden="true" className="size-5 shrink-0 text-ink-muted" />
                <span className="text-[0.875rem] font-medium text-ink">
                  {toolset.label || toolset.name}
                </span>
                <Badge tone={toolset.enabled ? "ok" : "neutral"}>
                  {toolset.enabled ? "active" : "inactive"}
                </Badge>
              </div>
              <p className="m-0 -mt-1 pl-8 text-[0.75rem] text-ink-muted">
                {toolset.description ||
                  `${toolset.tools.length} tool${toolset.tools.length === 1 ? "" : "s"}`}
              </p>
              {toolset.tools.length ? (
                <ul
                  aria-label={`${toolset.name} tools`}
                  className="m-0 flex list-none flex-wrap gap-1 py-0 pr-0 pl-8"
                >
                  {toolset.tools.map((tool) => (
                    <li key={tool}>
                      <Badge
                        tone="secondary"
                        className="type-data text-[0.6875rem] tracking-[0.12em] [font-stretch:100%]"
                      >
                        {tool}
                      </Badge>
                    </li>
                  ))}
                </ul>
              ) : null}
              <div className="pl-8">
                <Button size="sm" icon={<Wrench />} onClick={() => setOpen(toolset)}>
                  Configure
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
      {open ? <ToolsetDrawer toolset={open} onClose={() => setOpen(null)} /> : null}
    </QueryState>
  );
}
