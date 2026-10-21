// The Settings overlay (P20-32): navigation of views and child pages, a breadcrumb, search that
// lands on the owning child page, the "Applies to" profile chips (two or more profiles; reset when
// the active profile changes) and the page itself. The overlay route carries the place in its
// query (`/settings?view=…&page=…&field=…`), so links and search hits are the same thing.

import * as Dialog from "@radix-ui/react-dialog";
import { ChevronRight, Search, X } from "lucide-react";
import {
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { desktopBridge } from "../app/bridge.ts";
import { useGatewayConnection } from "../gateway/react.tsx";
import { useT } from "../i18n/index.tsx";
import { requestOnboarding } from "../onboarding/requests.ts";
import {
  attempt,
  type SaveKey,
  type SettingsBridge,
  SettingsContext,
  type SettingsContextValue,
} from "./context.tsx";
import { CustomSettingsPage } from "./customPages.tsx";
import { resolveTarget, targetQuery, VIEWS, type ViewDef, viewDef } from "./pages.ts";
import { SchemaPage } from "./schemaPage.tsx";
import { buildEntries, type SchemaFields, search } from "./search.ts";
import { bridgeRest, patchFor, type RestLike, rest } from "./settingsApi.ts";
import "./settings.css";

export interface ProfileRow {
  readonly name: string;
  readonly display_name?: string | null;
}

export interface SettingsOverlayProps {
  /** The overlay route's query (`?view=…`). */
  query: string;
  /** Replace the overlay route's query (stays on `/settings`). */
  onNavigate: (query: string) => void;
  onClose: () => void;
  /** Default: main's REST proxy. */
  api?: RestLike | null;
  /** Default: `window.indusDesktop`. */
  bridge?: SettingsBridge | null;
  /** Default: the window's daemon connection (`model.save_key`). */
  saveKey?: SaveKey | null;
  openOnboarding?: () => void;
}

function mergeValue(tree: unknown, key: string, value: unknown): unknown {
  const patch = patchFor(key, value);
  const merge = (a: unknown, b: unknown): unknown => {
    if (
      a === null ||
      typeof a !== "object" ||
      Array.isArray(a) ||
      b === null ||
      typeof b !== "object" ||
      Array.isArray(b)
    ) {
      return b;
    }
    const out: Record<string, unknown> = { ...(a as Record<string, unknown>) };
    for (const [k, v] of Object.entries(b as Record<string, unknown>)) out[k] = merge(out[k], v);
    return out;
  };
  return merge(tree ?? {}, patch);
}

/** Printable keys typed outside a field go to the search box. */
function isTypeToSearch(event: KeyboardEvent<HTMLElement>): boolean {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return false;
  if (event.key.length !== 1 || event.key === " ") return false;
  const target = event.target as HTMLElement | null;
  const tag = target?.tagName ?? "";
  return !["INPUT", "TEXTAREA", "SELECT"].includes(tag) && target?.isContentEditable !== true;
}

export function SettingsOverlay(props: SettingsOverlayProps) {
  const t = useT();
  const target = resolveTarget(props.query);
  const view = viewDef(target.view) as ViewDef;
  const page = view.pages.find((p) => p.id === target.page) ?? view.pages[0];
  const api = props.api === undefined ? bridgeRest() : props.api;
  const bridge: SettingsBridge | null =
    props.bridge === undefined ? (desktopBridge() ?? null) : props.bridge;
  const connection = useGatewayConnection();
  const saveKey: SaveKey | null =
    props.saveKey !== undefined
      ? props.saveKey
      : connection === null
        ? null
        : async (slug, apiKey) => {
            await connection.request("model.save_key", { slug, api_key: apiKey });
          };

  const [schema, setSchema] = useState<SchemaFields | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [config, setConfig] = useState<unknown>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<readonly ProfileRow[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [configTick, setConfigTick] = useState(0);
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let live = true;
    void rest<{ fields: SchemaFields }>(api, "GET", "/api/config/schema").then((answer) => {
      if (!live) return;
      if (answer.ok) setSchema(answer.data.fields ?? {});
      else setSchemaError(answer.message);
    });
    void rest<{ profiles: ProfileRow[]; current?: string }>(api, "GET", "/api/profiles").then(
      (answer) => {
        if (!live || !answer.ok) return;
        setProfiles(Array.isArray(answer.data.profiles) ? answer.data.profiles : []);
        if (typeof answer.data.current === "string") {
          setActive((now) => now ?? (answer.data.current as string));
        }
      },
    );
    return () => {
      live = false;
    };
  }, [api]);

  // The active profile: main's choice (P20-26); a change resets the chips to it.
  const profileBridge = bridge?.profile;
  useEffect(() => {
    void attempt(profileBridge ? () => profileBridge.get() : null).then((answer) => {
      if (answer.ok && answer.value.active !== null) setActive(answer.value.active);
    });
    return profileBridge?.onChanged((state) => {
      setActive(state.active);
      setChosen(null);
    });
  }, [profileBridge]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: configTick asks for a reload
  useEffect(() => {
    let live = true;
    setConfig(null);
    setConfigError(null);
    void rest<unknown>(api, "GET", "/api/config", { profile: chosen }).then((answer) => {
      if (!live) return;
      if (answer.ok) setConfig(answer.data);
      else setConfigError(answer.message);
    });
    return () => {
      live = false;
    };
  }, [api, chosen, configTick]);

  const entries = useMemo(() => buildEntries(schema, t), [schema, t]);
  const hits = useMemo(() => search(entries, query), [entries, query]);

  const go = useCallback(
    (next: { view: ViewDef["id"]; page?: string; field?: string | null; row?: string | null }) => {
      props.onNavigate(targetQuery(next));
    },
    [props.onNavigate],
  );

  const context: SettingsContextValue = {
    api,
    bridge,
    profile: chosen,
    activeProfile: active,
    saveKey,
    openOnboarding: props.openOnboarding ?? (() => requestOnboarding({ reason: "manual" })),
    config,
    reloadConfig: () => setConfigTick((n) => n + 1),
    focusRow: target.row,
  };

  const showChips = view.scoped && profiles.length >= 2;
  let content: ReactNode;
  if (page === undefined) content = null;
  else if (page.custom !== undefined) content = <CustomSettingsPage page={page.custom} />;
  else if (schemaError !== null) content = <p className="settings-row-error">{schemaError}</p>;
  else if (configError !== null) content = <p className="settings-row-error">{configError}</p>;
  else if (schema === null || config === null)
    content = <p className="settings-empty">{t("settings.loading")}</p>;
  else
    content = (
      <SchemaPage
        page={page}
        schema={schema}
        config={config}
        api={api}
        profile={chosen}
        focusField={target.field}
        onSaved={(key, value) => setConfig((now: unknown) => mergeValue(now, key, value))}
      />
    );

  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) props.onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="overlay-backdrop" />
        <Dialog.Content
          className="settings-overlay"
          aria-describedby={undefined}
          onKeyDown={(event) => {
            if (!isTypeToSearch(event)) return;
            event.preventDefault();
            setQuery((now) => now + event.key);
            searchRef.current?.focus();
          }}
        >
          <nav className="settings-nav" aria-label={t("settings.nav")}>
            <label className="settings-search">
              <Search aria-hidden="true" size={14} />
              <input
                ref={searchRef}
                type="search"
                aria-label={t("settings.search")}
                placeholder={t("settings.search")}
                value={query}
                onChange={(event) => setQuery(event.currentTarget.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape" && query !== "") {
                    event.preventDefault();
                    event.stopPropagation();
                    setQuery("");
                  }
                }}
              />
            </label>
            {query.trim() !== "" ? (
              <ul className="settings-hits" aria-label={t("settings.results")}>
                {hits.length === 0 && <li className="settings-empty">{t("settings.noResults")}</li>}
                {hits.map((hit) => (
                  <li key={hit.id}>
                    <button
                      type="button"
                      className="settings-hit"
                      onClick={() => {
                        setQuery("");
                        go(hit.target);
                      }}
                    >
                      <span className="settings-hit-label">{hit.label}</span>
                      <span className="settings-hit-context">{hit.context}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <ul className="settings-views">
                {VIEWS.map((item) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      className="settings-view"
                      aria-current={item.id === view.id ? "page" : undefined}
                      onClick={() => go({ view: item.id })}
                    >
                      {t(item.label)}
                    </button>
                    {item.id === view.id && item.pages.length > 1 && (
                      <ul className="settings-pages">
                        {item.pages.map((child) => (
                          <li key={child.id}>
                            <button
                              type="button"
                              className="settings-page-link"
                              aria-current={child.id === page?.id ? "page" : undefined}
                              onClick={() => go({ view: item.id, page: child.id })}
                            >
                              {t(child.label)}
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </nav>
          <section className="settings-main">
            <header className="settings-header">
              <Dialog.Title asChild>
                <ol className="settings-breadcrumb" aria-label={t("settings.breadcrumb")}>
                  <li>
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => go({ view: VIEWS[0]?.id ?? "model" })}
                    >
                      {t("nav.settings")}
                    </button>
                  </li>
                  <li>
                    <ChevronRight aria-hidden="true" size={12} />
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => go({ view: view.id })}
                    >
                      {t(view.label)}
                    </button>
                  </li>
                  {page !== undefined && (
                    <li aria-current="page">
                      <ChevronRight aria-hidden="true" size={12} />
                      <span>{t(page.label)}</span>
                    </li>
                  )}
                </ol>
              </Dialog.Title>
              <Dialog.Close className="icon-button" aria-label={t("common.close")}>
                <X aria-hidden="true" size={16} />
              </Dialog.Close>
            </header>
            {showChips && (
              <div className="settings-scope">
                <span className="settings-scope-label" id="settings-scope-label">
                  {t("settings.scope.appliesTo")}
                </span>
                <div
                  className="settings-chips"
                  role="radiogroup"
                  aria-labelledby="settings-scope-label"
                >
                  {profiles.map((profile) => {
                    const selected = (chosen ?? active) === profile.name;
                    return (
                      // biome-ignore lint/a11y/useSemanticElements: chips, not native radios
                      <button
                        key={profile.name}
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        className="settings-chip"
                        onClick={() => setChosen(profile.name === active ? null : profile.name)}
                      >
                        {profile.display_name || profile.name}
                      </button>
                    );
                  })}
                </div>
                {chosen !== null && (
                  <p className="settings-row-note" role="status">
                    {t("settings.scope.editing", { profile: chosen })}
                  </p>
                )}
              </div>
            )}
            <SettingsContext.Provider value={context}>
              <div
                className="settings-content"
                key={`${view.id}:${page?.id ?? ""}:${chosen ?? ""}`}
              >
                {content}
              </div>
            </SettingsContext.Provider>
          </section>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
