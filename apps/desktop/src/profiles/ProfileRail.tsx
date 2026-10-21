// The profile rail (P20-26): one coloured square per profile, the foreground one marked, the
// desktop's default badged. A click swaps the window to that profile (an explicit selection: a
// fresh draft); the context menu (right click, the context-menu key, Shift+F10) opens the profile
// in a new window or makes it the default.

import { palette, type RampName } from "@indus/ui";
import { Star } from "lucide-react";
import {
  type KeyboardEvent,
  type MouseEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useT } from "../i18n/index.tsx";
import "./profiles.css";

/** Ramps used for profile squares (the neutral one is left out), two steps each. */
const RAMPS: readonly RampName[] = ["river", "clay", "reed", "ochre", "kiln"];
const STEPS = [500, 700] as const;

/** A stable colour for a profile name. */
export function profileColor(name: string): string {
  let hash = 0;
  for (const char of name) hash = (Math.imul(hash, 31) + (char.codePointAt(0) ?? 0)) >>> 0;
  const ramp = RAMPS[hash % RAMPS.length] as RampName;
  const step = STEPS[Math.floor(hash / RAMPS.length) % STEPS.length] as (typeof STEPS)[number];
  return palette[ramp][step].css;
}

export function profileInitial(name: string): string {
  return (name.trim()[0] ?? "?").toUpperCase();
}

/** The profiles the backend knows (`GET /api/profiles`), sorted with `default` first. */
export function profileNames(body: unknown): string[] {
  const list = (body as { profiles?: unknown } | null)?.profiles;
  if (!Array.isArray(list)) return [];
  const names = list
    .map((item) => (item as { name?: unknown } | null)?.name)
    .filter((name): name is string => typeof name === "string" && name !== "");
  return [...new Set(names)].sort((a, b) =>
    a === "default" ? -1 : b === "default" ? 1 : a.localeCompare(b),
  );
}

export type ProfilesApi = (payload: {
  method: "GET";
  path: string;
  profile?: string;
}) => Promise<{ kind: "response" | "not-found"; status: number; body: unknown }>;

/** Load the profile list (again when `refreshKey` changes). */
export function useProfileNames(api: ProfilesApi | null, refreshKey: unknown): string[] {
  const [names, setNames] = useState<string[]>([]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey asks for a reload
  useEffect(() => {
    if (api === null) return;
    let live = true;
    api({ method: "GET", path: "/api/profiles" }).then(
      (answer) => {
        if (live && answer.kind === "response" && answer.status < 400) {
          setNames(profileNames(answer.body));
        }
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [api, refreshKey]);
  return names;
}

export interface ProfileRailProps {
  profiles: readonly string[];
  /** The profile the window shows. */
  foreground: string;
  /** The profile being switched to, if any. */
  pending?: string | null;
  /** The desktop's default profile (badged). */
  defaultProfile: string | null;
  onSelect(profile: string): void;
  onOpenInWindow(profile: string): void;
  onSetDefault(profile: string): void;
}

interface MenuState {
  profile: string;
  x: number;
  y: number;
}

function RailMenu(props: {
  menu: MenuState;
  isDefault: boolean;
  onClose(): void;
  onOpenInWindow(): void;
  onSetDefault(): void;
}) {
  const t = useT();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    const close = (event: Event) => {
      if (
        ref.current !== null &&
        event.target instanceof Node &&
        ref.current.contains(event.target)
      ) {
        return;
      }
      props.onClose();
    };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [props.onClose]);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = [
      ...(ref.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []),
    ];
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Escape" || event.key === "Tab") {
      event.preventDefault();
      props.onClose();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      items[(index + step + items.length) % items.length]?.focus();
    }
  };
  return (
    <div
      ref={ref}
      role="menu"
      aria-label={t("profiles.menu", { profile: props.menu.profile })}
      className="rail-menu"
      style={{ left: props.menu.x, top: props.menu.y }}
      onKeyDown={onKeyDown}
    >
      <button
        type="button"
        role="menuitem"
        onClick={() => {
          props.onOpenInWindow();
          props.onClose();
        }}
      >
        {t("profiles.openInWindow")}
      </button>
      <button
        type="button"
        role="menuitem"
        aria-disabled={props.isDefault}
        onClick={() => {
          if (!props.isDefault) props.onSetDefault();
          props.onClose();
        }}
      >
        {props.isDefault ? t("profiles.isDefault") : t("profiles.setDefault")}
      </button>
    </div>
  );
}

export function ProfileRail(props: ProfileRailProps) {
  const t = useT();
  const [menu, setMenu] = useState<MenuState | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  const openMenu = (profile: string, x: number, y: number) => setMenu({ profile, x, y });

  const onContextMenu = (event: MouseEvent<HTMLButtonElement>, profile: string) => {
    event.preventDefault();
    openMenu(profile, event.clientX, event.clientY);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, profile: string) => {
    if (event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey)) {
      event.preventDefault();
      const rect = event.currentTarget.getBoundingClientRect();
      openMenu(profile, rect.right, rect.top);
    }
  };

  return (
    <nav className="profile-rail" aria-label={t("profiles.rail")}>
      <ul>
        {props.profiles.map((profile) => {
          const current = profile === props.foreground;
          const pending = profile === props.pending;
          const isDefault = profile === (props.defaultProfile ?? "default");
          const label = [
            t("profiles.switchTo", { profile }),
            isDefault ? t("profiles.defaultSuffix") : "",
          ]
            .filter((part) => part !== "")
            .join(" ");
          return (
            <li key={profile}>
              <button
                type="button"
                className="rail-square"
                aria-label={label}
                aria-pressed={current}
                aria-busy={pending ? true : undefined}
                aria-haspopup="menu"
                title={profile}
                data-pending={pending ? "true" : undefined}
                style={{ background: profileColor(profile) }}
                onClick={() => {
                  if (!current) props.onSelect(profile);
                }}
                onContextMenu={(event) => onContextMenu(event, profile)}
                onKeyDown={(event) => onKeyDown(event, profile)}
              >
                <span aria-hidden="true">{profileInitial(profile)}</span>
                {isDefault && <Star aria-hidden="true" size={9} className="rail-default" />}
              </button>
            </li>
          );
        })}
      </ul>
      {menu !== null && (
        <RailMenu
          menu={menu}
          isDefault={menu.profile === (props.defaultProfile ?? "default")}
          onClose={closeMenu}
          onOpenInWindow={() => props.onOpenInWindow(menu.profile)}
          onSetDefault={() => props.onSetDefault(menu.profile)}
        />
      )}
    </nav>
  );
}
