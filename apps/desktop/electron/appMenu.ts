// The application menu template (built on ready). Standard edit roles so copy/paste work in every
// window, our own zoom items (the whole-window scale, not Chromium's per-page zoom), and window
// roles. DevTools items exist only in dev builds.

export interface MenuItem {
  label?: string;
  role?: string;
  type?: "separator" | "submenu" | "normal";
  accelerator?: string;
  click?: () => void;
  submenu?: MenuItem[];
}

export interface AppMenuInput {
  appName: string;
  platform: string;
  dev: boolean;
  zoom: (direction: "in" | "out" | "reset") => void;
  newWindow: () => void;
}

const separator: MenuItem = { type: "separator" };

export function appMenuTemplate(input: AppMenuInput): MenuItem[] {
  const mac = input.platform === "darwin";
  const menu: MenuItem[] = [];
  if (mac) {
    menu.push({
      label: input.appName,
      submenu: [
        { role: "about" },
        separator,
        { role: "services" },
        separator,
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        separator,
        { role: "quit" },
      ],
    });
  }
  menu.push({
    label: "File",
    submenu: [
      { label: "New Window", accelerator: "CommandOrControl+Shift+N", click: input.newWindow },
      separator,
      mac ? { role: "close" } : { role: "quit" },
    ],
  });
  menu.push({
    label: "Edit",
    submenu: [
      { role: "undo" },
      { role: "redo" },
      separator,
      { role: "cut" },
      { role: "copy" },
      { role: "paste" },
      { role: "pasteAndMatchStyle" },
      { role: "delete" },
      { role: "selectAll" },
    ],
  });
  const view: MenuItem[] = [
    { label: "Actual Size", accelerator: "CommandOrControl+0", click: () => input.zoom("reset") },
    { label: "Zoom In", accelerator: "CommandOrControl+=", click: () => input.zoom("in") },
    { label: "Zoom Out", accelerator: "CommandOrControl+-", click: () => input.zoom("out") },
    separator,
    { role: "togglefullscreen" },
  ];
  if (input.dev) view.push(separator, { role: "reload" }, { role: "toggleDevTools" });
  menu.push({ label: "View", submenu: view });
  menu.push({
    label: "Window",
    submenu: mac
      ? [{ role: "minimize" }, { role: "zoom" }, separator, { role: "front" }]
      : [{ role: "minimize" }, { role: "close" }],
  });
  return menu;
}
