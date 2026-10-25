// The read-only YAML view (W52-pc-10): the settings the config file itself sets, from the
// effective entries whose source is the file, nested by key and written as YAML. Values arrive
// redacted. There is no raw-file endpoint yet, so nothing here writes.

import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import { api } from "../../lib/api.ts";
import { nestEntries, toYaml } from "../../lib/yaml.ts";
import { Card, CardHeader, Loading, QueryState } from "../../ui/controls.tsx";

export const EMPTY_YAML = "# config.yaml has no settings yet\n";

export function YamlView() {
  const effective = useQuery({
    queryKey: ["config", "effective"],
    queryFn: () => api.config.effective(),
  });
  const fromFile = (effective.data?.entries ?? []).filter((entry) => entry.source === "file");
  const text = toYaml(nestEntries(fromFile)) || EMPTY_YAML;
  return (
    <Card padded={false}>
      <CardHeader icon={FileText} title="Raw YAML configuration" />
      {effective.isPending ? (
        <div className="flex justify-center py-12">
          <Loading />
        </div>
      ) : (
        <QueryState
          loading={false}
          error={effective.error}
          onRetry={() => void effective.refetch()}
        >
          <textarea
            aria-label="Raw YAML configuration"
            readOnly
            spellCheck={false}
            value={text}
            className="type-data block min-h-[600px] w-full resize-y border-0 bg-transparent px-4 py-3 text-[0.8125rem] leading-[21px] text-ink outline-none"
          />
        </QueryState>
      )}
    </Card>
  );
}
