// Pages and overlays that 1.0 reserves but does not build yet (N15/N16 and later phases):
// an empty state for pages, a dismissable card for overlays.

import * as Dialog from "@radix-ui/react-dialog";
import { Archive, Bot, Clock, MessagesSquare, Puzzle, Settings, Users, X } from "lucide-react";
import type { ReactNode } from "react";
import { type MessageKey, useT } from "../i18n/index.tsx";
import type { OverlayId, PageId } from "./routes.ts";

export const PAGE_TEXT: Readonly<Record<PageId, { title: MessageKey; body: MessageKey }>> = {
  capabilities: { title: "nav.capabilities", body: "page.capabilities.body" },
  messaging: { title: "nav.messaging", body: "page.messaging.body" },
  artifacts: { title: "nav.artifacts", body: "page.artifacts.body" },
};

export const OVERLAY_TEXT: Readonly<Record<OverlayId, { title: MessageKey; body: MessageKey }>> = {
  settings: { title: "nav.settings", body: "overlay.settings.body" },
  cron: { title: "nav.cron", body: "overlay.cron.body" },
  profiles: { title: "nav.profiles", body: "overlay.profiles.body" },
  agents: { title: "nav.agents", body: "overlay.agents.body" },
};

export const PAGE_ICONS: Readonly<Record<PageId, ReactNode>> = {
  capabilities: <Puzzle aria-hidden="true" size={16} />,
  messaging: <MessagesSquare aria-hidden="true" size={16} />,
  artifacts: <Archive aria-hidden="true" size={16} />,
};

export const OVERLAY_ICONS: Readonly<Record<OverlayId, ReactNode>> = {
  settings: <Settings aria-hidden="true" size={16} />,
  cron: <Clock aria-hidden="true" size={16} />,
  profiles: <Users aria-hidden="true" size={16} />,
  agents: <Bot aria-hidden="true" size={16} />,
};

export function ComingLaterPage(props: { page: PageId }) {
  const t = useT();
  const text = PAGE_TEXT[props.page];
  return (
    <section className="page-empty" aria-labelledby={`page-${props.page}`}>
      <span className="page-empty-icon">{PAGE_ICONS[props.page]}</span>
      <h1 id={`page-${props.page}`}>{t(text.title)}</h1>
      <p>{t(text.body)}</p>
      <p className="page-empty-tag">{t("common.comingLater")}</p>
    </section>
  );
}

export function OverlayCard(props: { overlay: OverlayId; onClose: () => void }) {
  const t = useT();
  const text = OVERLAY_TEXT[props.overlay];
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) props.onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="overlay-backdrop" />
        <Dialog.Content className="overlay-card">
          <header className="overlay-header">
            <Dialog.Title>{t(text.title)}</Dialog.Title>
            <Dialog.Close className="icon-button" aria-label={t("common.close")}>
              <X aria-hidden="true" size={16} />
            </Dialog.Close>
          </header>
          <Dialog.Description>{t(text.body)}</Dialog.Description>
          <p className="page-empty-tag">{t("common.comingLater")}</p>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function LaterWindow() {
  const t = useT();
  return (
    <main className="later-window">
      <p>{t("window.later.title")}</p>
    </main>
  );
}
