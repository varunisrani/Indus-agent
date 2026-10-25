// The sidebar row's menu (W52-h): right-click a session row, or Shift+F10 / the context-menu key on
// a focused one. Only actions Indus really has: open in a floating window, rename, pin, unread,
// copy the id, branch (`session.branch`, W54-D1-8), export (main writes the file, W56-G1),
// archive, delete (W52-h-4 lists what the reference has that we leave out).

import {
  Archive,
  ArchiveRestore,
  Copy,
  Download,
  ExternalLink,
  GitBranch,
  Mail,
  MailOpen,
  Pencil,
  Pin,
  PinOff,
  Trash2,
} from "lucide-react";
import { useT } from "../i18n/index.tsx";
import { ContextMenuAt, MenuRow, MenuSeparator } from "../ui/components.tsx";

export interface RowMenuActions {
  popOut?: (() => void) | undefined;
  rename: () => void;
  togglePin: () => void;
  toggleUnread: () => void;
  copyId: () => void;
  /** Start a new chat from this one's history (absent: no connection to ask). */
  branch?: (() => void) | undefined;
  /** Save the conversation to a file the person picks (absent: no desktop bridge). */
  exportFile?: (() => void) | undefined;
  toggleArchive: () => void;
  remove: () => void;
}

export function SessionRowMenu(props: {
  x: number;
  y: number;
  title: string;
  pinned: boolean;
  unread: boolean;
  archived: boolean;
  actions: RowMenuActions;
  onClose: () => void;
}) {
  const t = useT();
  const run = (action: () => void) => () => {
    props.onClose();
    action();
  };
  const a = props.actions;
  return (
    <ContextMenuAt
      label={t("sidebar.menu.label", { title: props.title })}
      x={props.x}
      y={props.y}
      onClose={props.onClose}
    >
      {a.popOut !== undefined && (
        <>
          <MenuRow
            icon={<ExternalLink aria-hidden="true" />}
            label={t("sidebar.menu.popOut")}
            onSelect={run(a.popOut)}
          />
          <MenuSeparator />
        </>
      )}
      <MenuRow
        icon={<Pencil aria-hidden="true" />}
        label={t("sidebar.menu.rename")}
        onSelect={run(a.rename)}
      />
      <MenuRow
        icon={props.pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}
        label={t(props.pinned ? "sidebar.menu.unpin" : "sidebar.menu.pin")}
        onSelect={run(a.togglePin)}
      />
      <MenuRow
        icon={props.unread ? <MailOpen aria-hidden="true" /> : <Mail aria-hidden="true" />}
        label={t(props.unread ? "sidebar.menu.read" : "sidebar.menu.unread")}
        onSelect={run(a.toggleUnread)}
      />
      <MenuRow
        icon={<Copy aria-hidden="true" />}
        label={t("sidebar.menu.copyId")}
        onSelect={run(a.copyId)}
      />
      {(a.branch !== undefined || a.exportFile !== undefined) && <MenuSeparator />}
      {a.branch !== undefined && (
        <MenuRow
          icon={<GitBranch aria-hidden="true" />}
          label={t("sidebar.menu.branch")}
          onSelect={run(a.branch)}
        />
      )}
      {a.exportFile !== undefined && (
        <MenuRow
          icon={<Download aria-hidden="true" />}
          label={t("sidebar.menu.export")}
          onSelect={run(a.exportFile)}
        />
      )}
      <MenuSeparator />
      <MenuRow
        icon={
          props.archived ? <ArchiveRestore aria-hidden="true" /> : <Archive aria-hidden="true" />
        }
        label={t(props.archived ? "sidebar.menu.unarchive" : "sidebar.menu.archive")}
        onSelect={run(a.toggleArchive)}
      />
      <MenuRow
        icon={<Trash2 aria-hidden="true" />}
        label={t("sidebar.menu.delete")}
        tone="danger"
        onSelect={run(a.remove)}
      />
    </ContextMenuAt>
  );
}
