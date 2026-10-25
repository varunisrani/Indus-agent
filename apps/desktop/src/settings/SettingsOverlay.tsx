// The Settings overlay (P20-32): navigation of views and child pages, a breadcrumb, search that
// lands on the owning child page, the "Applies to" profile chips (two or more profiles; reset when
// the active profile changes) and the page itself. The overlay route carries the place in its
// query (`/settings?view=…&page=…&field=…`), so links and search hits are the same thing.

import * as Dialog from "@radix-ui/react-dialog";
import { ChevronRight, Download, RefreshCw, Search, Upload, X } from "lucide-react";
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
import { pageIcon, startsGroup, viewIcon } from "./navMeta.tsx";
import { resolveTarget, targetQuery, VIEWS, type ViewDef, viewDef } from "./pages.ts";
import { SchemaPage } from "./schemaPage.tsx";
import { buildEntries, type SchemaFields, search } from "./search.ts";
import { bridgeRest, patchFor, type RestLike, rest } from "./settingsApi.ts";
import "../app/overlay/overlay.css";
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
  return mergePatch(tree, patchFor(key, value));
}

/** The form tree with `patch` laid over it (objects merge, anything else replaces). */
function mergePatch(tree: unknown, patch: Record<string, unknown>): unknown {
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
  // groups opened by their caret (the current view's group is always open)
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const toggleOpened = (id: string) =>
    setOpened((now) => {
      const next = new Set(now);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
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
    // settings slice A (W52-i)
    schema,
    focusField: target.field,
    saveField: async (key, value) => {
      const answer = await rest<{ ok: boolean }>(api, "PUT", "/api/config", {
        body: { config: patchFor(key, value) },
        profile: chosen,
      });
      if (!answer.ok) return answer.message || t("settings.saveFailed");
      setConfig((now: unknown) => mergeValue(now, key, value));
      return null;
    },
    savePatch: async (patch) => {
      const answer = await rest<{ ok: boolean }>(api, "PUT", "/api/config", {
        body: { config: patch },
        profile: chosen,
      });
      if (!answer.ok) return answer.message || t("settings.saveFailed");
      setConfig((now: unknown) => mergePatch(now, patch));
      return null;
    },
    go: (next) => go(next),
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
      <>
        {/* a hand-built block above the schema rows (W60-L2-2: enabled toolsets) */}
        {page.before !== undefined && <CustomSettingsPage page={page.before} />}
        <SchemaPage
          page={page}
          schema={schema}
          config={config}
          api={api}
          profile={chosen}
          focusField={target.field}
          onSaved={(key, value) => setConfig((now: unknown) => mergeValue(now, key, value))}
        />
        {/* a hand-built block under the schema rows (W52-j: wake word, archived sessions) */}
        {page.after !== undefined && <CustomSettingsPage page={page.after} />}
      </>
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
          className="desk-overlay-card settings-overlay"
          aria-describedby={undefined}
          // the search pill stays a pill until it is used: typing anywhere still reaches it (W52-i)
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            (event.currentTarget as HTMLElement | null)?.focus?.();
          }}
          onKeyDown={(event) => {
            if (!isTypeToSearch(event)) return;
            event.preventDefault();
            setQuery((now) => now + event.key);
            searchRef.current?.focus();
          }}
        >
          <nav className="settings-nav desk-overlay-nav" aria-label={t("settings.nav")}>
            <label className="settings-search">
              <Search aria-hidden="true" size={12} />
              <input
                ref={searchRef}
                type="search"
                aria-label={t("settings.search")}
                placeholder={t("settingsA.searchPill")}
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
                {VIEWS.map((item) => {
                  const current = item.id === view.id;
                  const nested = item.pages.length > 1;
                  const expanded = nested && (current || opened.has(item.id));
                  return (
                    <li
                      key={item.id}
                      className="desk-nav-entry"
                      data-gap={startsGroup(item.id) ? "true" : undefined}
                    >
                      <div className="settings-view-line">
                        <button
                          type="button"
                          className="desk-nav-link settings-view"
                          aria-current={current ? "page" : undefined}
                          onClick={() => go({ view: item.id })}
                        >
                          {viewIcon(item.id)}
                          <span className="desk-nav-label">{t(item.label)}</span>
                        </button>
                        {nested && (
                          <button
                            type="button"
                            className="settings-caret"
                            aria-expanded={expanded}
                            aria-label={t(expanded ? "settings.collapse" : "settings.expand", {
                              section: t(item.label),
                            })}
                            onClick={() => toggleOpened(item.id)}
                          >
                            <ChevronRight aria-hidden="true" size={13} />
                          </button>
                        )}
                      </div>
                      {expanded && (
                        <ul className="settings-pages">
                          {item.pages.map((child) => (
                            <li key={child.id} className="desk-nav-entry" data-depth="1">
                              <button
                                type="button"
                                className="desk-nav-link settings-page-link"
                                aria-current={current && child.id === page?.id ? "page" : undefined}
                                onClick={() => go({ view: item.id, page: child.id })}
                              >
                                {pageIcon(item.id, child.id)}
                                <span className="desk-nav-label">{t(child.label)}</span>
                              </button>
                            </li>
                          ))}
                        </ul>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            {/* settings slice A (W52-i): export / import / reset, not wired in this build */}
            <div className="settings-rail-footer">
              {(
                [
                  ["settingsA.footer.export", <Download key="e" aria-hidden="true" size={14} />],
                  ["settingsA.footer.import", <Upload key="i" aria-hidden="true" size={14} />],
                  ["settingsA.footer.reset", <RefreshCw key="r" aria-hidden="true" size={14} />],
                ] as const
              ).map(([key, icon]) => (
                <button
                  key={key}
                  type="button"
                  className="desk-icon-btn"
                  aria-label={t(key)}
                  title={`${t(key)} · ${t("settings.notAvailable")}`}
                  disabled
                >
                  {icon}
                </button>
              ))}
            </div>
          </nav>
          <section className="settings-main">
            <header className="settings-header">
              <Dialog.Title asChild>
                <ol
                  className="settings-breadcrumb desk-breadcrumb"
                  aria-label={t("settings.breadcrumb")}
                >
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
              <Dialog.Close
                className="desk-icon-btn desk-overlay-close"
                aria-label={t("common.close")}
              >
                <X aria-hidden="true" size={14} />
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
