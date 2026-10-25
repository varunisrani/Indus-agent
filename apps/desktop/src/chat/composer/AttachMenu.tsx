// The composer's `+` menu (W52-h): files, a folder reference, images, a URL reference and prompt
// snippets. Files and images become attachments (the existing local / remote path rules apply);
// a folder or URL is inserted as an `@folder:` / `@url:` reference the backend expands. Paste
// image is shown disabled: the renderer cannot read clipboard images through our bridge (Ctrl+V
// in the editor still attaches them).

import {
  ClipboardPaste,
  File,
  FolderOpen,
  Image,
  Link,
  MessageSquareText,
  Plus,
  ScanSearch,
  Wand,
} from "lucide-react";
import { type FormEvent, useCallback, useRef, useState } from "react";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import {
  Button,
  MenuLabel,
  MenuPanel,
  MenuRow,
  MenuSeparator,
  SmallDialog,
} from "../../ui/components.tsx";

export type PickMode = "files" | "folder" | "images";

/** The `@folder:` reference for a picked folder: relative to the chat's folder when inside it. */
export function folderReference(path: string, cwd: string | null): string {
  let shown = path;
  if (cwd !== null && cwd !== "") {
    const base = cwd.endsWith("/") ? cwd : `${cwd}/`;
    if (path === cwd) shown = ".";
    else if (path.startsWith(base)) shown = path.slice(base.length);
  }
  return `@folder:${shown.includes(" ") ? `"${shown}"` : shown}`;
}

/** A URL a reference may carry (http or https only), or null. */
export function cleanUrl(text: string): string | null {
  try {
    const url = new URL(text.trim());
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

export const SNIPPETS: ReadonlyArray<{
  id: string;
  icon: typeof File;
  label: MessageKey;
  hint: MessageKey;
  text: MessageKey;
}> = [
  {
    id: "review",
    icon: ScanSearch,
    label: "attach.snippet.review",
    hint: "attach.snippet.reviewHint",
    text: "attach.snippet.reviewText",
  },
  {
    id: "plan",
    icon: Wand,
    label: "attach.snippet.plan",
    hint: "attach.snippet.planHint",
    text: "attach.snippet.planText",
  },
  {
    id: "explain",
    icon: MessageSquareText,
    label: "attach.snippet.explain",
    hint: "attach.snippet.explainHint",
    text: "attach.snippet.explainText",
  },
];

export function AttachMenu(props: {
  /** Ask main for paths; resolves with the chosen ones (empty when cancelled). */
  pick: ((mode: PickMode) => Promise<string[]>) | null;
  /** Picked files / images become attachments. */
  onFiles: (paths: string[]) => void;
  /** Text inserted at the caret (references, snippets). */
  onInsert: (text: string) => void;
  cwd: string | null;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<"url" | "snippets" | null>(null);
  const [url, setUrl] = useState("");
  const [urlError, setUrlError] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  const choose = (run: () => void) => {
    setOpen(false);
    run();
  };
  const pickInto = async (mode: PickMode) => {
    if (props.pick === null) return;
    const paths = await props.pick(mode);
    if (paths.length === 0) return;
    if (mode === "folder") {
      props.onInsert(paths.map((path) => folderReference(path, props.cwd)).join(" "));
    } else props.onFiles(paths);
  };
  const submitUrl = (event: FormEvent) => {
    event.preventDefault();
    const clean = cleanUrl(url);
    if (clean === null) {
      setUrlError(true);
      return;
    }
    props.onInsert(`@url:${clean}`);
    setDialog(null);
    setUrl("");
    setUrlError(false);
  };
  const noPicker = props.pick === null;
  return (
    <div className="desk-menu-root composer-attach-root">
      <button
        ref={trigger}
        type="button"
        className="desk-icon-btn composer-attach"
        aria-label={t("composer.attach")}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Plus aria-hidden="true" />
      </button>
      {open && (
        <MenuPanel
          label={t("attach.menu")}
          onClose={close}
          keepOpenFor={trigger}
          className="composer-attach-menu"
        >
          <MenuLabel>{t("attach.menu")}</MenuLabel>
          <MenuRow
            icon={<File aria-hidden="true" />}
            label={t("attach.files")}
            disabled={noPicker}
            onSelect={() => choose(() => void pickInto("files"))}
          />
          <MenuRow
            icon={<FolderOpen aria-hidden="true" />}
            label={t("attach.folder")}
            disabled={noPicker}
            onSelect={() => choose(() => void pickInto("folder"))}
          />
          <MenuRow
            icon={<Image aria-hidden="true" />}
            label={t("attach.images")}
            disabled={noPicker}
            onSelect={() => choose(() => void pickInto("images"))}
          />
          <MenuRow
            icon={<ClipboardPaste aria-hidden="true" />}
            label={t("attach.paste")}
            disabled
            onSelect={() => undefined}
          />
          <MenuRow
            icon={<Link aria-hidden="true" />}
            label={t("attach.url")}
            onSelect={() => choose(() => setDialog("url"))}
          />
          <MenuSeparator />
          <MenuRow
            icon={<MessageSquareText aria-hidden="true" />}
            label={t("attach.snippets")}
            onSelect={() => choose(() => setDialog("snippets"))}
          />
          <MenuSeparator />
          <p className="composer-attach-tip">
            {t("attach.tipBefore")} <kbd className="desk-kbd">@</kbd> {t("attach.tipAfter")}
          </p>
        </MenuPanel>
      )}
      {dialog === "url" && (
        <SmallDialog
          title={t("attach.urlTitle")}
          description={t("attach.urlHint")}
          onClose={() => setDialog(null)}
        >
          <form className="composer-url-form" onSubmit={submitUrl}>
            <input
              className="desk-input"
              type="url"
              aria-label={t("attach.urlField")}
              placeholder="https://"
              value={url}
              aria-invalid={urlError}
              onChange={(event) => {
                setUrl(event.target.value);
                setUrlError(false);
              }}
            />
            {urlError && (
              <p role="alert" className="composer-url-error">
                {t("attach.urlInvalid")}
              </p>
            )}
            <div className="desk-dialog-actions">
              <Button variant="ghost" onClick={() => setDialog(null)}>
                {t("attach.cancel")}
              </Button>
              <Button variant="primary" type="submit">
                {t("attach.urlAdd")}
              </Button>
            </div>
          </form>
        </SmallDialog>
      )}
      {dialog === "snippets" && (
        <SmallDialog
          title={t("attach.snippetsTitle")}
          description={t("attach.snippetsHint")}
          onClose={() => setDialog(null)}
        >
          <ul className="composer-snippets">
            {SNIPPETS.map((snippet) => (
              <li key={snippet.id}>
                <button
                  type="button"
                  className="composer-snippet"
                  onClick={() => {
                    props.onInsert(t(snippet.text));
                    setDialog(null);
                  }}
                >
                  <snippet.icon aria-hidden="true" size={16} />
                  <span>
                    <strong>{t(snippet.label)}</strong>
                    <small>{t(snippet.hint)}</small>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </SmallDialog>
      )}
    </div>
  );
}
