// Which Indus profile the management pages read and write (R09.20). The choice lives in the URL as
// `?profile=<name>`; the REST client appends it to every profile-scoped call. Switching remounts the
// routed pages with a fresh query cache so nothing keeps showing the previous profile's data.

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { createContext, type ReactNode, useContext, useState } from "react";
import { useSearchParams } from "react-router";
import { activeProfile, api, setSelectedProfile } from "../lib/api.ts";
import { initialProfile } from "../lib/bootstrap.ts";
import type { ProfileSummary } from "../lib/restTypes.ts";

interface ProfileScope {
  /** The name from the URL, or "" for the backend's own profile. */
  selected: string;
  /** The profile calls actually target (selection, else the serving profile). */
  active: string;
  profiles: ProfileSummary[];
  defaultName: string;
  select(name: string): void;
  /** `?profile=<name>` (or "") to keep the scope on links. */
  search: string;
  /** Fetch the profile list again (after a create, rename or delete). */
  refresh(): void;
}

const ProfileContext = createContext<ProfileScope>({
  selected: "",
  active: "",
  profiles: [],
  defaultName: "",
  select: () => {},
  search: "",
  refresh: () => {},
});

export function useProfileScope(): ProfileScope {
  return useContext(ProfileContext);
}

export function ProfileScopeProvider({ children }: { children: ReactNode }) {
  const [params, setParams] = useSearchParams();
  const selected = (params.get("profile") ?? initialProfile()).trim();
  // Set synchronously: child pages issue their first requests before any parent effect runs.
  setSelectedProfile(selected);
  const active = activeProfile();

  const profiles = useQuery({
    queryKey: ["shell", "profiles"],
    queryFn: () => api.profiles(),
    staleTime: 30_000,
  });
  const list = profiles.data?.profiles ?? [];
  const defaultName = list.find((p) => p.is_default)?.name ?? "";

  const select = (name: string) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (name) next.set("profile", name);
        else next.delete("profile");
        return next;
      },
      { replace: false },
    );
  };

  const value: ProfileScope = {
    selected,
    active,
    profiles: list,
    defaultName,
    select,
    search: selected ? `?profile=${encodeURIComponent(selected)}` : "",
    refresh: () => void profiles.refetch(),
  };
  return <ProfileContext.Provider value={value}>{children}</ProfileContext.Provider>;
}

function freshClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
}

/** Remounts `children` with its own query cache whenever the active profile changes. */
export function ProfileKeyed({ children }: { children: ReactNode }) {
  const { active } = useProfileScope();
  return <ProfileCache key={active || "(serving)"}>{children}</ProfileCache>;
}

function ProfileCache({ children }: { children: ReactNode }) {
  const [client] = useState(freshClient);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** Profile picker, shown only when the machine has more than one profile. */
export function ProfileSwitcher({ compact }: { compact: boolean }) {
  const { profiles, active, select, defaultName } = useProfileScope();
  if (profiles.length <= 1) return null;
  return (
    <label
      className={`type-label flex flex-col gap-1 border-b border-line px-5 py-2.5 text-ink-soft ${compact ? "sr-only" : ""}`}
    >
      Profile
      <select
        className="type-data h-7 border border-line bg-canvas px-2 text-[0.75rem] text-ink normal-case tracking-normal"
        value={active || defaultName}
        onChange={(event) => select(event.currentTarget.value)}
      >
        {profiles.map((p) => (
          <option key={p.name} value={p.name}>
            {p.name}
            {p.is_default ? " (default)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Amber notice while a non-default profile is being managed. */
export function ProfileBanner() {
  const { active, defaultName } = useProfileScope();
  if (!active || !defaultName || active === defaultName) return null;
  return (
    <div
      role="status"
      className="border-b border-warning/50 bg-warning-wash px-4 py-2 text-[0.8125rem] text-warning lg:px-[1.375rem]"
    >
      Managing profile <strong>{active}</strong>: changes here apply to that profile, not the
      default one.
    </div>
  );
}
