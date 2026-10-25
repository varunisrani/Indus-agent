// Modal dialogs on Radix primitives (focus trap, Escape, labelled by title and description).
// Look: a square panel over a dimmed, blurred page; wide uppercase title, serif description, a
// close cross, and large typewriter buttons at the bottom right.

import * as RadixDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "./controls.tsx";
import { cx } from "./cx.ts";

export type ModalSize = "sm" | "narrow" | "md" | "lg";

const widths: Record<ModalSize, string> = {
  sm: "w-[min(22.5rem,92vw)]",
  /** Short forms such as "Add MCP server" (W52-c). */
  narrow: "w-[min(30rem,92vw)]",
  md: "w-[min(36rem,92vw)]",
  lg: "w-[min(45rem,94vw)]",
};

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  size = "md",
  className,
  titleClassName,
  descriptionClassName,
  bodyClassName,
  overlayClassName,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** One serif line under the title; leave out for plain form panels (W52-c). */
  description?: ReactNode;
  size?: ModalSize;
  /** Extra classes for the panel, title, description, body and scrim (W52-a: the model picker's
   * raised, taller panel with its own column layout). Leave unset for the standard dialog. */
  className?: string;
  titleClassName?: string;
  descriptionClassName?: string;
  bodyClassName?: string;
  overlayClassName?: string;
  children: ReactNode;
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay
          className={cx(
            "fixed inset-0 z-40 [animation:indus-fade-in_160ms_ease-out]",
            overlayClassName ?? "bg-overlay backdrop-blur-[2px]",
          )}
        />
        <RadixDialog.Content
          {...(description ? {} : { "aria-describedby": undefined })}
          className={cx(
            "fixed top-1/2 left-1/2 z-50 max-h-[88vh] -translate-x-1/2 -translate-y-1/2 overflow-y-auto border border-line-strong bg-canvas text-ink shadow-2xl [animation:indus-pop-in_180ms_ease-out]",
            widths[size],
            className,
          )}
        >
          <div className="flex items-start gap-3 border-b border-line px-4 pt-3.5 pb-3">
            <div className="min-w-0 flex-1">
              <RadixDialog.Title
                className={titleClassName ?? "type-section-title m-0 text-[0.9375rem]"}
              >
                {title}
              </RadixDialog.Title>
              {description ? (
                <RadixDialog.Description
                  className={
                    descriptionClassName ??
                    "type-serif m-0 mt-1 text-[0.8125rem] leading-snug text-ink-soft"
                  }
                >
                  {description}
                </RadixDialog.Description>
              ) : null}
            </div>
            <RadixDialog.Close
              aria-label="Close dialog"
              className="-mr-1 inline-flex size-6 items-center justify-center text-ink-soft hover:text-ink"
            >
              <X className="size-3.5" />
            </RadixDialog.Close>
          </div>
          <div className={bodyClassName ?? "px-4 py-3"}>{children}</div>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

/** The "PanelModal" role named by the wave-52 specs: the same component. */
export const PanelModal = Modal;

/** The bottom-right button row of a dialog. */
export function DialogActions({ children }: { children: ReactNode }) {
  return <div className="mt-3 flex justify-end gap-2">{children}</div>;
}

/** A yes/no question; `onConfirm` runs only on the confirm button. */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "Confirm",
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
  children,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children?: ReactNode;
}) {
  return (
    <Modal
      open={open}
      size="sm"
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
      title={title}
      description={description}
    >
      {children}
      <DialogActions>
        <Button size="lg" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="lg" tone={danger ? "danger" : "primary"} disabled={busy} onClick={onConfirm}>
          {confirmLabel}
        </Button>
      </DialogActions>
    </Modal>
  );
}
