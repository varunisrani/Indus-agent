// Launch knobs read before `ready`: Chromium only copies switches such as `--js-flags` to its
// renderers from the browser's own command line, so they must be appended before the app starts,
// and a packaged app opened from a launcher has no wrapper script to pass them. We read two keys
// from the profile's config.yaml without a YAML library:
//
//   desktop:
//     electron_flags: ["--enable-features=X", "--js-flags=--expose-gc"]   # or a block list:
//     electron_flags:
//       - --disable-gpu-vsync
//     renderer_max_old_space_mb: 4096
//
// Only that shape is understood: `desktop:` at column 0, keys indented by two spaces, block list
// items by four. Anything else leaves the defaults; when the block plainly tried to set one of the
// keys, one warning says which shape is supported.

export interface LaunchConfig {
  electronFlags: string[];
  rendererMaxOldSpaceMb: number;
}

export interface LaunchConfigRead {
  config: LaunchConfig;
  warnings: string[];
}

export interface PlannedSwitch {
  name: string;
  value?: string;
}

const KEY_LINE = /^ {2}([a-z_]+):(.*)$/;
const ITEM_LINE = /^ {4}- (.*)$/;

function stripComment(value: string): string {
  // A `#` starts a comment only after whitespace (or at the start), never inside a quoted string.
  let quote: string | null = null;
  for (let i = 0; i < value.length; i += 1) {
    const ch = value[i];
    if (quote !== null) {
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === "#" && (i === 0 || /\s/.test(value[i - 1] ?? ""))) {
      return value.slice(0, i);
    }
  }
  return value;
}

function scalar(raw: string): string {
  const text = raw.trim();
  const quoted = /^(["'])(.*)\1$/.exec(text);
  return quoted ? (quoted[2] ?? "") : text;
}

function flowList(text: string): string[] | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("[") || !trimmed.endsWith("]")) return null;
  return trimmed
    .slice(1, -1)
    .split(",")
    .map(scalar)
    .filter((item) => item !== "");
}

/** Read the launch knobs from config.yaml text. Never throws. */
export function readLaunchConfig(yamlText: string): LaunchConfigRead {
  const config: LaunchConfig = { electronFlags: [], rendererMaxOldSpaceMb: 0 };
  const warnings: string[] = [];
  const lines = String(yamlText ?? "").split(/\r?\n/);
  const start = lines.findIndex((line) => /^desktop:\s*(?:#.*)?$/.test(line));
  if (start < 0) return { config, warnings };

  let mentioned = false;
  let understood = false;
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i] ?? "";
    if (line.trim() === "" || /^\s*#/.test(line)) continue;
    if (/^\S/.test(line)) break;
    if (/\b(?:electron_flags|renderer_max_old_space_mb)\s*:/.test(line)) mentioned = true;
    const key = KEY_LINE.exec(line);
    if (key === null) continue;
    const name = key[1];
    const value = stripComment(key[2] ?? "").trim();
    if (name === "renderer_max_old_space_mb") {
      const mb = Number(scalar(value));
      if (Number.isInteger(mb) && mb > 0) {
        config.rendererMaxOldSpaceMb = mb;
        understood = true;
      }
    } else if (name === "electron_flags") {
      const flow = flowList(value);
      if (flow !== null) {
        config.electronFlags = flow;
      } else if (value !== "") {
        config.electronFlags = scalar(value).split(/\s+/).filter(Boolean);
      } else {
        const items: string[] = [];
        while (i + 1 < lines.length) {
          const item = ITEM_LINE.exec(lines[i + 1] ?? "");
          if (item === null) break;
          const text = scalar(stripComment(item[1] ?? ""));
          if (text !== "") items.push(text);
          i += 1;
        }
        config.electronFlags = items;
      }
      if (config.electronFlags.length > 0) understood = true;
    }
  }
  if (mentioned && !understood) {
    warnings.push(
      "config.yaml: desktop.electron_flags / desktop.renderer_max_old_space_mb ignored; " +
        "write them as two-space keys under a top-level `desktop:` (list items indented four " +
        "spaces with `- `)",
    );
  }
  return { config, warnings };
}

/** Split argv into `[name, value?]` switches, joining `--name value` pairs. */
function argvSwitches(argv: readonly string[]): Array<[string, string | undefined]> {
  const out: Array<[string, string | undefined]> = [];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = String(argv[i] ?? "").trim();
    const match = /^--([^=\s]+)(?:=(.*))?$/.exec(arg);
    if (match === null) continue;
    const name = match[1] ?? "";
    let value = match[2];
    const next = String(argv[i + 1] ?? "").trim();
    // `--js-flags` always takes a value (its value itself starts with dashes); other switches
    // take the next token only when it is not a switch.
    if (value === undefined && next !== "" && (name === "js-flags" || !next.startsWith("-"))) {
      value = next;
      i += 1;
    }
    out.push([name, value]);
  }
  return out;
}

/**
 * The `appendSwitch` calls for this launch. Every `--js-flags` source (argv, config flags, the
 * heap ceiling) is merged into one switch — appending it twice would keep only the last. A switch
 * already on argv is not appended again, and a `--max-old-space-size` there beats the config's.
 */
export function planSwitches(config: LaunchConfig, argv: readonly string[]): PlannedSwitch[] {
  const jsFlags: string[] = [];
  const seen = new Set<string>();
  const planned: PlannedSwitch[] = [];
  const addJs = (value: string | undefined) => {
    for (const part of String(value ?? "").split(/\s+/)) if (part) jsFlags.push(part);
  };
  for (const [name, value] of argvSwitches(argv)) {
    if (name === "js-flags") addJs(value);
    else seen.add(name);
  }
  for (const [name, value] of argvSwitches(config.electronFlags)) {
    if (name === "js-flags") {
      addJs(value);
    } else if (!seen.has(name)) {
      seen.add(name);
      planned.push(value === undefined ? { name } : { name, value });
    }
  }
  if (
    config.rendererMaxOldSpaceMb > 0 &&
    !jsFlags.some((flag) => flag.startsWith("--max-old-space-size="))
  ) {
    jsFlags.push(`--max-old-space-size=${config.rendererMaxOldSpaceMb}`);
  }
  if (jsFlags.length > 0)
    planned.push({ name: "js-flags", value: [...new Set(jsFlags)].join(" ") });
  return planned;
}

export interface OzoneInput {
  platform: string;
  env: Readonly<Record<string, string | undefined>>;
  argv: readonly string[];
}

/**
 * `wayland` when a Linux session is Wayland (native or WSLg) and nothing chose a backend
 * explicitly; null to leave Chromium's default. An SSH session that forwards only X stays on X11.
 */
export function chooseOzonePlatform({ platform, env, argv }: OzoneInput): "wayland" | null {
  if (platform !== "linux") return null;
  if (argv.some((arg) => /^--ozone-platform(?:-hint)?(?:=|$)/.test(String(arg)))) return null;
  if ((env.ELECTRON_OZONE_PLATFORM_HINT ?? "").trim() !== "") return null;
  const waylandDisplay = (env.WAYLAND_DISPLAY ?? "").trim();
  const sessionType = (env.XDG_SESSION_TYPE ?? "").trim().toLowerCase();
  const wsl = Boolean(env.WSL_DISTRO_NAME || env.WSL_INTEROP);
  if (wsl) return waylandDisplay !== "" ? "wayland" : null;
  if (waylandDisplay === "") return null;
  return sessionType === "" || sessionType === "wayland" ? "wayland" : null;
}
