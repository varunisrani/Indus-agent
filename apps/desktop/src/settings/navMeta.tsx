// How each Settings view shows in the left menu (W51-b): its icon and whether a gap opens above
// it. Views a later wave adds fall back to a plain dot icon and no gap.

import {
  Archive,
  Bell,
  Box,
  Brain,
  CaseSensitive,
  Circle,
  CircleUser,
  Cpu,
  Download,
  FileText,
  FolderOpen,
  Globe,
  Info,
  Keyboard,
  KeyRound,
  LayoutGrid,
  Lock,
  MessageCircle,
  Mic,
  Monitor,
  Network,
  Palette,
  PawPrint,
  Settings2,
  ShieldCheck,
  SquareTerminal,
  Trash2,
  Users,
  Volume2,
  Wrench,
  Zap,
} from "lucide-react";
import type { ReactNode } from "react";

const ICONS: Readonly<Record<string, ReactNode>> = {
  model: <Box aria-hidden="true" />,
  chat: <MessageCircle aria-hidden="true" />,
  appearance: <Palette aria-hidden="true" />,
  workspace: <Monitor aria-hidden="true" />,
  safety: <Lock aria-hidden="true" />,
  browser: <Globe aria-hidden="true" />, // settings slice A (W52-i)
  passwords: <ShieldCheck aria-hidden="true" />, // settings slice A (W52-i)
  memory: <Brain aria-hidden="true" />,
  advanced: <Wrench aria-hidden="true" />,
  notifications: <Bell aria-hidden="true" />,
  providers: <Zap aria-hidden="true" />,
  gateways: <Globe aria-hidden="true" />,
  voice: <Mic aria-hidden="true" />,
  tools: <KeyRound aria-hidden="true" />,
  keyboard: <Keyboard aria-hidden="true" />,
  sessions: <Archive aria-hidden="true" />,
  plugins: <LayoutGrid aria-hidden="true" />,
  about: <Info aria-hidden="true" />,
};

/** Views that start a new group in the menu (an 8px gap above them). */
const GROUP_STARTS: ReadonlySet<string> = new Set(["providers", "plugins", "about"]);

export function viewIcon(view: string): ReactNode {
  return ICONS[view] ?? <Circle aria-hidden="true" />;
}

export function startsGroup(view: string): boolean {
  return GROUP_STARTS.has(view);
}

// -- child page icons (14px in the rail): settings A (W52-i) and settings B (W52-j) ---------------

const PAGE_ICONS: Readonly<Record<string, ReactNode>> = {
  "model/main": <Box aria-hidden="true" />,
  "model/fallbacks": <Box aria-hidden="true" />,
  "model/auxiliary": <Cpu aria-hidden="true" />,
  "model/moa": <Users aria-hidden="true" />,
  "chat/behaviour": <MessageCircle aria-hidden="true" />,
  "appearance/general": <Settings2 aria-hidden="true" />,
  "appearance/theme": <Palette aria-hidden="true" />,
  "appearance/typography": <CaseSensitive aria-hidden="true" />,
  "appearance/window": <Monitor aria-hidden="true" />,
  "appearance/chat-display": <MessageCircle aria-hidden="true" />,
  "appearance/pet": <PawPrint aria-hidden="true" />,
  "workspace/projects": <FolderOpen aria-hidden="true" />,
  "workspace/shell": <SquareTerminal aria-hidden="true" />,
  "workspace/files": <FileText aria-hidden="true" />,
  "safety/approvals": <ShieldCheck aria-hidden="true" />,
  "safety/privacy": <Lock aria-hidden="true" />,
  "safety/checkpoints": <Archive aria-hidden="true" />,
  "browser/profile": <Globe aria-hidden="true" />,
  "browser/network": <Network aria-hidden="true" />,
  "passwords/credentials": <KeyRound aria-hidden="true" />,
  "passwords/sources": <Lock aria-hidden="true" />,
  "memory/persistent": <Brain aria-hidden="true" />,
  "memory/context": <FileText aria-hidden="true" />,
  "voice/conversation": <Mic aria-hidden="true" />,
  "voice/transcription": <FileText aria-hidden="true" />,
  "voice/speech": <Volume2 aria-hidden="true" />,
  "advanced/desktop": <Monitor aria-hidden="true" />,
  "advanced/runtime": <Cpu aria-hidden="true" />,
  "advanced/tools": <Wrench aria-hidden="true" />,
  "advanced/terminal": <SquareTerminal aria-hidden="true" />,
  "advanced/delegation": <Users aria-hidden="true" />,
  "advanced/output": <FileText aria-hidden="true" />,
  "advanced/profiles": <Users aria-hidden="true" />,
  "notifications/alerts": <Bell aria-hidden="true" />,
  "notifications/sounds": <Volume2 aria-hidden="true" />,
  "providers/accounts": <CircleUser aria-hidden="true" />,
  "providers/keys": <KeyRound aria-hidden="true" />,
  "providers/endpoints": <Globe aria-hidden="true" />,
  "providers/local": <Cpu aria-hidden="true" />,
  "gateways/connection": <Monitor aria-hidden="true" />,
  "gateways/connections": <Globe aria-hidden="true" />,
  "gateways/updates": <Download aria-hidden="true" />,
  "keyboard/shortcuts": <Keyboard aria-hidden="true" />,
  "keyboard/global": <Keyboard aria-hidden="true" />,
  "tools/keys": <KeyRound aria-hidden="true" />,
  "sessions/archived": <Archive aria-hidden="true" />,
  "sessions/directory": <FolderOpen aria-hidden="true" />,
  "about/version": <Download aria-hidden="true" />,
  "about/uninstall": <Trash2 aria-hidden="true" />,
  "about/licences": <FileText aria-hidden="true" />,
};

/** A child page's icon in the rail, or null (no icon, the label keeps its indent). */
export function pageIcon(view: string, page: string): ReactNode {
  return PAGE_ICONS[`${view}/${page}`] ?? null;
}
