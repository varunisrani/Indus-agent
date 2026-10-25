// The Effective view (ours): every setting's value as the agent sees it and where it comes from.

import { useQuery } from "@tanstack/react-query";
import { ListChecks } from "lucide-react";
import { api } from "../../lib/api.ts";
import type { ValueSource } from "../../lib/restTypes.ts";
import {
  Badge,
  type BadgeTone,
  Card,
  CardHeader,
  CountChip,
  QueryState,
} from "../../ui/controls.tsx";
import { displayValue } from "./ConfigField.tsx";

const SOURCE_TONE: Record<ValueSource, BadgeTone> = {
  default: "secondary",
  file: "accent",
  env: "warn",
  overlay: "danger",
};

const SOURCE_LABEL: Record<ValueSource, string> = {
  default: "default",
  file: "config file",
  env: "environment",
  overlay: "admin overlay",
};

const headClass = "type-label h-8 px-0 text-left text-[0.6875rem] font-normal text-ink-soft";

export function EffectiveView() {
  const effective = useQuery({
    queryKey: ["config", "effective"],
    queryFn: () => api.config.effective(),
  });
  const entries = effective.data?.entries ?? [];
  return (
    <Card padded={false}>
      <CardHeader
        icon={ListChecks}
        title="Effective configuration"
        aside={effective.data ? <CountChip>{entries.length}</CountChip> : null}
      />
      <div className="px-4 pb-4">
        <QueryState
          loading={effective.isPending}
          error={effective.error}
          empty={entries.length === 0}
          emptyText="No settings reported."
          onRetry={() => void effective.refetch()}
        >
          <table className="w-full border-collapse" aria-label="Effective configuration">
            <thead>
              <tr>
                <th className={headClass}>Key</th>
                <th className={`${headClass} pl-4`}>Value</th>
                <th className={`${headClass} w-px pl-4 text-right whitespace-nowrap`}>Source</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr
                  key={entry.key}
                  className="border-t border-line"
                  data-testid={`effective-${entry.key}`}
                >
                  <td className="type-data py-2 align-top text-[0.75rem] text-ink">{entry.key}</td>
                  <td className="type-data py-2 pl-4 align-top text-[0.75rem] break-all text-ink-muted">
                    {displayValue(entry.value)}
                  </td>
                  <td className="w-px py-2 pl-4 text-right align-top whitespace-nowrap">
                    <Badge tone={SOURCE_TONE[entry.source]} className="whitespace-nowrap">
                      {SOURCE_LABEL[entry.source]}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </QueryState>
      </div>
    </Card>
  );
}
