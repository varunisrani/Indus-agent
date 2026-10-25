// The `dashboard.show_token_analytics` config flag (W52-b-3): token figures are a local estimate,
// so the Analytics nav item and the token numbers on Analytics and Models show only when it is on.
// Shares the shell's config query, so pages and the sidebar read one cached copy per profile.

import { useQuery } from "@tanstack/react-query";
import { useProfileScope } from "../shell/ProfileScope.tsx";
import { api } from "./api.ts";
import type { ConfigObject } from "./restTypes.ts";

export function analyticsFlag(config: ConfigObject | undefined): boolean {
  const dashboard = config?.dashboard;
  if (typeof dashboard !== "object" || dashboard === null || Array.isArray(dashboard)) return false;
  return (dashboard as ConfigObject).show_token_analytics === true;
}

/** `enabled` is the flag; `settled` turns true once the config has loaded (or failed). */
export function useTokenAnalyticsFlag(): { enabled: boolean; settled: boolean } {
  const scope = useProfileScope();
  const config = useQuery({
    queryKey: ["shell", "config", scope.active],
    queryFn: () => api.config.get(),
    staleTime: 60_000,
  });
  return { enabled: analyticsFlag(config.data), settled: !config.isPending };
}
