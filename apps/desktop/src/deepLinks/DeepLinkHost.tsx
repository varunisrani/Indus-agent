// Receives deep links in the main window (P20-34): listens on `deepLinks.onLink`, then tells main
// it listens (`deepLinks.ready`) so a link that came during boot is delivered exactly once.
// `open` navigates inside the app; `skill` and `mcp` ask first and only then call the backend.

import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { desktopBridge } from "../app/bridge.ts";
import { overlayStateFrom } from "../app/routes.ts";
import type { IndusDesktopDeepLinks } from "../global.d.ts";
import { useT } from "../i18n/index.tsx";
import { bridgeRest, type RestLike, rest } from "../settings/settingsApi.ts";
import { type LinkAction, type LinkPayload, planLink } from "./deepLinkActions.ts";

type Pending = Extract<LinkAction, { kind: "confirm-skill" | "confirm-mcp" }>;

export interface DeepLinkHostProps {
  /** Default: the bridge's `deepLinks` group (none in a plain browser). */
  links?: Pick<IndusDesktopDeepLinks, "onLink" | "ready"> | null;
  api?: RestLike | null;
}

export function DeepLinkHost(props: DeepLinkHostProps) {
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  const links = props.links === undefined ? (desktopBridge()?.deepLinks ?? null) : props.links;
  const api = props.api === undefined ? bridgeRest() : props.api;
  const [pending, setPending] = useState<Pending | null>(null);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [incoming, setIncoming] = useState<LinkPayload | null>(null);

  useEffect(() => {
    if (links === null) return;
    const off = links.onLink((payload) => setIncoming(payload));
    links.ready();
    return off;
  }, [links]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: act once per arriving link
  useEffect(() => {
    if (incoming === null) return;
    setIncoming(null);
    const action = planLink(incoming);
    switch (action.kind) {
      case "navigate":
        navigate(
          action.path,
          action.overlay ? { state: overlayStateFrom(location.pathname, location.state) } : {},
        );
        return;
      case "confirm-skill":
      case "confirm-mcp":
        setNote(null);
        setPending(action);
        return;
      case "refused":
        setNote({ text: t(action.reason), error: true });
        return;
      case "ignore":
        return;
    }
  }, [incoming]);

  const confirm = async () => {
    if (pending === null) return;
    setBusy(true);
    const answer =
      pending.kind === "confirm-skill"
        ? await rest(api, "POST", "/api/skills/hub/install", {
            body: { identifier: pending.identifier },
            timeoutMs: 120_000,
          })
        : await rest(api, "POST", "/api/mcp/servers", {
            body: { name: pending.name, url: pending.url, auth: pending.auth },
          });
    setBusy(false);
    setPending(null);
    setNote(
      answer.ok
        ? {
            text: t(pending.kind === "confirm-skill" ? "deepLink.skill.done" : "deepLink.mcp.done"),
            error: false,
          }
        : { text: answer.message, error: true },
    );
  };

  return (
    <>
      {pending !== null && (
        <Dialog.Root
          open
          onOpenChange={(open) => {
            if (!open && !busy) setPending(null);
          }}
        >
          <Dialog.Portal>
            <Dialog.Overlay className="overlay-backdrop" />
            <Dialog.Content className="overlay-card" aria-describedby="deep-link-body">
              <Dialog.Title>
                {t(
                  pending.kind === "confirm-skill" ? "deepLink.skill.title" : "deepLink.mcp.title",
                )}
              </Dialog.Title>
              <p id="deep-link-body">
                {t(pending.kind === "confirm-skill" ? "deepLink.skill.body" : "deepLink.mcp.body")}
              </p>
              <code className="settings-code deep-link-target">
                {pending.kind === "confirm-skill"
                  ? pending.identifier
                  : `${pending.name} — ${pending.url}`}
              </code>
              <div className="onboarding-actions">
                <Dialog.Close className="button button-secondary" disabled={busy}>
                  {t("deepLink.cancel")}
                </Dialog.Close>
                <button
                  type="button"
                  className="button button-primary"
                  disabled={busy}
                  onClick={() => void confirm()}
                >
                  {t(
                    pending.kind === "confirm-skill"
                      ? "deepLink.skill.install"
                      : "deepLink.mcp.add",
                  )}
                </button>
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      )}
      {note !== null && (
        <div className="toast" role={note.error ? "alert" : "status"}>
          <span>{note.text}</span>
          <button type="button" className="icon-button" onClick={() => setNote(null)}>
            {t("common.close")}
          </button>
        </div>
      )}
    </>
  );
}
