// The small icon in front of a session, drawn in its source's colour: a prompt for terminals, a
// speech bubble for chat apps, a hash for Discord, a clock for schedules, a play mark for tool
// runs, a globe for web and API callers, a database for ACP (W52-a source table).

import {
  Clock,
  Database,
  Globe,
  Hash,
  MessageCircle,
  MessageSquare,
  Play,
  SquareTerminal,
  Terminal,
} from "lucide-react";
import { cx } from "../../ui/cx.ts";
import { type SourceKind, sourceKind } from "./sources.ts";

type Icon = typeof Clock;

interface SourceLook {
  icon: Icon;
  colour: string;
}

const BY_SOURCE: Readonly<Record<string, SourceLook>> = {
  cli: { icon: SquareTerminal, colour: "text-ink" },
  tui: { icon: SquareTerminal, colour: "text-ink" },
  desktop: { icon: SquareTerminal, colour: "text-ink" },
  telegram: { icon: MessageCircle, colour: "text-info" },
  matrix: { icon: MessageCircle, colour: "text-info" },
  discord: { icon: Hash, colour: "text-[#8b7cf6]" },
  slack: { icon: MessageSquare, colour: "text-[#2bb3a3]" },
  email: { icon: MessageSquare, colour: "text-[#2bb3a3]" },
  whatsapp: { icon: MessageCircle, colour: "text-success" },
  signal: { icon: MessageCircle, colour: "text-success" },
  sms: { icon: Globe, colour: "text-success" },
  cron: { icon: Clock, colour: "text-warning" },
  tool: { icon: Play, colour: "text-warning" },
  flows: { icon: Play, colour: "text-warning" },
  oneshot: { icon: Terminal, colour: "text-warning" },
  webhook: { icon: Globe, colour: "text-warning" },
  api_server: { icon: Globe, colour: "text-ink-soft" },
  acp: { icon: Database, colour: "text-ink-soft" },
  web: { icon: Globe, colour: "text-ink" },
};

const BY_KIND: Record<SourceKind, SourceLook> = {
  terminal: { icon: SquareTerminal, colour: "text-ink" },
  web: { icon: Globe, colour: "text-ink" },
  automation: { icon: Clock, colour: "text-warning" },
  messaging: { icon: MessageCircle, colour: "text-info" },
  unknown: { icon: Globe, colour: "text-ink-soft" },
};

export function sourceLook(source: string | null): SourceLook {
  return (source ? BY_SOURCE[source] : undefined) ?? BY_KIND[sourceKind(source)];
}

/** The source's icon; `tinted` (default) draws it in the source colour. */
export function SourceIcon({
  source,
  className,
  tinted = true,
}: {
  source: string | null;
  className?: string;
  tinted?: boolean;
}) {
  const look = sourceLook(source);
  const Icon = look.icon;
  return <Icon aria-hidden="true" className={cx(className ?? "size-3.5", tinted && look.colour)} />;
}
