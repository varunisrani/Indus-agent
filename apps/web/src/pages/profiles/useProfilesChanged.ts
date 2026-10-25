// After a profile write: refetch every profile query and the shell's profile list.

import { useQueryClient } from "@tanstack/react-query";
import { useProfileScope } from "../../shell/ProfileScope.tsx";

export const PROFILES_KEY = ["profiles", "list"] as const;

export function useProfilesChanged(): () => void {
  const client = useQueryClient();
  const scope = useProfileScope();
  return () => {
    void client.invalidateQueries({ queryKey: ["profiles"] });
    scope.refresh();
  };
}
