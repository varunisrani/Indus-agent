// The Blueprints tab of Cron (W52-c): jobs that installed skills suggest in their front matter.
// "Set up" opens the job panel prefilled with the suggestion; nothing is scheduled until Create.

import { useQuery } from "@tanstack/react-query";
import { WandSparkles } from "lucide-react";
import { api } from "../../lib/api.ts";
import type { CronBlueprint } from "../../lib/restTypes.ts";
import { errorText } from "../../shell/Toasts.tsx";
import { Badge, Button, Card, Loading } from "../../ui/controls.tsx";

export function Blueprints({ onSetUp }: { onSetUp: (blueprint: CronBlueprint) => void }) {
  const query = useQuery({
    queryKey: ["cron", "blueprints"],
    queryFn: () => api.cron.blueprints(),
  });
  if (query.isPending)
    return (
      <div className="flex justify-center py-12">
        <Loading>Loading blueprints…</Loading>
      </div>
    );
  if (query.error)
    return (
      <p role="alert" className="m-0 text-[0.8125rem] text-danger">
        {errorText(query.error)}
      </p>
    );
  const list = query.data.blueprints;
  if (list.length === 0)
    return (
      <Card className="py-8 text-center">
        <p className="m-0 text-[0.8125rem] text-ink-muted">
          No blueprints yet. Skills that suggest a scheduled job show up here.
        </p>
      </Card>
    );
  return (
    <ul aria-label="Blueprints" className="m-0 grid list-none grid-cols-1 gap-3 p-0 lg:grid-cols-2">
      {list.map((blueprint) => (
        <li
          key={blueprint.skill}
          data-testid={`blueprint-${blueprint.skill}`}
          className="flex items-start gap-3 border border-line p-4"
        >
          <div className="flex min-w-0 flex-1 flex-col">
            <h3 className="type-serif m-0 flex items-center gap-2 text-[0.9375rem] font-normal text-ink">
              <WandSparkles aria-hidden="true" className="size-4 shrink-0 text-ink-muted" />
              {blueprint.skill}
            </h3>
            {blueprint.description ? (
              <p className="type-serif m-0 mt-1.5 text-[0.8125rem] text-ink-muted">
                {blueprint.description}
              </p>
            ) : null}
            <div className="mt-2 flex flex-wrap gap-1">
              <Badge tone="secondary">{blueprint.skill}</Badge>
              {blueprint.schedule ? <Badge tone="secondary">{blueprint.schedule}</Badge> : null}
            </div>
          </div>
          <Button tone="primary" size="sm" onClick={() => onSetUp(blueprint)}>
            Set up
          </Button>
        </li>
      ))}
    </ul>
  );
}
