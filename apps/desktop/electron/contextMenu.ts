// The native context menu and the Linux find shortcut (P20-39).
//
// Right-click anywhere (app windows and preview guests alike) builds a menu from what Chromium
// reports: spelling suggestions and "Add to Dictionary" for a misspelled word in an editable
// field, the edit actions its flags allow, "Copy Link" for http/https/mailto links (copied as text;
// no menu item ever opens a link, D-16.20), "Copy Image", and "Inspect Element" in dev builds.
//
// On Linux some desktops take Ctrl+F before the page sees it, so main claims it for app windows
// and tells that window to open its find bar (`indus:find:open`); macOS and Windows leave the key
// to the renderer.

import type { MenuItem } from "./appMenu.ts";

export const MAX_SPELLING_SUGGESTIONS = 5;
const COPYABLE_LINK = /^(?:https?:|mailto:)/i;

export interface ContextMenuParamsLike {
  x: number;
  y: number;
  isEditable: boolean;
  misspelledWord: string;
  dictionarySuggestions: string[];
  selectionText: string;
  linkURL: string;
  mediaType: string;
  hasImageContents?: boolean;
  editFlags: {
    canCut: boolean;
    canCopy: boolean;
    canPaste: boolean;
    canSelectAll: boolean;
  };
}

export interface MenuContentsLike {
  readonly id: number;
  on(event: string, listener: (...args: never[]) => void): unknown;
  replaceMisspelling(word: string): void;
  cut(): void;
  copy(): void;
  paste(): void;
  selectAll(): void;
  copyImageAt(x: number, y: number): void;
  inspectElement(x: number, y: number): void;
  isDestroyed(): boolean;
  readonly session: { addWordToSpellCheckerDictionary(word: string): boolean };
}

export interface ContextMenuOptions {
  dev: boolean;
  writeText: (text: string) => void;
}

const separator: MenuItem = { type: "separator" };

/** The menu for one right-click; empty when there is nothing to offer. */
export function contextMenuTemplate(
  params: ContextMenuParamsLike,
  contents: MenuContentsLike,
  options: ContextMenuOptions,
): MenuItem[] {
  const groups: MenuItem[][] = [];

  if (params.isEditable && params.misspelledWord.trim() !== "") {
    const suggestions = params.dictionarySuggestions.slice(0, MAX_SPELLING_SUGGESTIONS);
    const word = params.misspelledWord;
    groups.push([
      ...(suggestions.length === 0
        ? [{ label: "No Spelling Suggestions", enabled: false } as MenuItem]
        : suggestions.map((suggestion) => ({
            label: suggestion,
            click: () => contents.replaceMisspelling(suggestion),
          }))),
      {
        label: "Add to Dictionary",
        click: () => {
          contents.session.addWordToSpellCheckerDictionary(word);
        },
      },
    ]);
  }

  const edit: MenuItem[] = [];
  const flags = params.editFlags;
  if (params.isEditable && flags.canCut) edit.push({ label: "Cut", click: () => contents.cut() });
  if (flags.canCopy && (params.isEditable || params.selectionText.trim() !== "")) {
    edit.push({ label: "Copy", click: () => contents.copy() });
  }
  if (params.isEditable && flags.canPaste) {
    edit.push({ label: "Paste", click: () => contents.paste() });
  }
  if (flags.canSelectAll && params.isEditable) {
    edit.push({ label: "Select All", click: () => contents.selectAll() });
  }
  if (edit.length > 0) groups.push(edit);

  const extra: MenuItem[] = [];
  const link = params.linkURL.trim();
  if (link !== "" && COPYABLE_LINK.test(link)) {
    extra.push({ label: "Copy Link", click: () => options.writeText(link) });
  }
  if (params.mediaType === "image" && params.hasImageContents !== false) {
    extra.push({ label: "Copy Image", click: () => contents.copyImageAt(params.x, params.y) });
  }
  if (extra.length > 0) groups.push(extra);

  if (options.dev) {
    groups.push([
      { label: "Inspect Element", click: () => contents.inspectElement(params.x, params.y) },
    ]);
  }

  const menu: MenuItem[] = [];
  for (const group of groups) {
    if (menu.length > 0) menu.push(separator);
    menu.push(...group);
  }
  return menu;
}

export interface ContextMenuDeps extends ContextMenuOptions {
  /** Show a template as a native popup over `contents` (`Menu.buildFromTemplate(…).popup`). */
  popup(template: MenuItem[], contents: MenuContentsLike): void;
}

/** Attach the context menu to one web contents. */
export function installContextMenu(contents: MenuContentsLike, deps: ContextMenuDeps): void {
  contents.on("context-menu", ((_event: unknown, params: ContextMenuParamsLike) => {
    if (contents.isDestroyed()) return;
    const template = contextMenuTemplate(params, contents, deps);
    if (template.length > 0) deps.popup(template, contents);
  }) as (...args: never[]) => void);
}

export interface KeyInputLike {
  type: string;
  key: string;
  control: boolean;
  meta: boolean;
  alt: boolean;
  shift: boolean;
  isAutoRepeat?: boolean;
}

/** Ctrl+F (no other modifier) on Linux; nothing elsewhere. */
export function isLinuxFindShortcut(input: KeyInputLike, platform: string): boolean {
  return (
    platform === "linux" &&
    input.type === "keyDown" &&
    input.isAutoRepeat !== true &&
    input.control &&
    !input.alt &&
    !input.meta &&
    !input.shift &&
    input.key.toLowerCase() === "f"
  );
}

export interface FindShortcutDeps {
  platform: string;
  /** Only windows the window manager made (not preview guests). */
  isAppWindow(contentsId: number): boolean;
  openFind(contents: MenuContentsLike): void;
}

/** Claim the Linux Ctrl+F for app windows and ask the window to open its find bar. */
export function installFindShortcut(contents: MenuContentsLike, deps: FindShortcutDeps): void {
  if (deps.platform !== "linux") return;
  contents.on("before-input-event", ((event: { preventDefault(): void }, input: KeyInputLike) => {
    if (!isLinuxFindShortcut(input, deps.platform) || !deps.isAppWindow(contents.id)) return;
    event.preventDefault();
    deps.openFind(contents);
  }) as (...args: never[]) => void);
}

export interface ContextMenusInstall {
  dev: boolean;
  platform: string;
  /** `app.on("web-contents-created", …)`. */
  onWebContentsCreated(listener: (contents: MenuContentsLike) => void): void;
  popup(template: MenuItem[], contents: MenuContentsLike): void;
  writeText(text: string): void;
  isAppWindow(contentsId: number): boolean;
  /** Push `indus:find:open` to that window (the registry's emit). */
  emitFindOpen(contents: MenuContentsLike): void;
}

/** One install step for main: context menus everywhere, the Linux find shortcut in app windows. */
export function installContextMenus(deps: ContextMenusInstall): void {
  deps.onWebContentsCreated((contents) => {
    installContextMenu(contents, deps);
    installFindShortcut(contents, {
      platform: deps.platform,
      isAppWindow: deps.isAppWindow,
      openFind: deps.emitFindOpen,
    });
  });
}
