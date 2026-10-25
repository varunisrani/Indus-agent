// Browse hub (P18-22, look W52-c): search the skills hub; every result carries the safety
// scanner's verdict and a `dangerous` skill cannot be installed.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useState } from "react";
import { ApiError, api } from "../../lib/api.ts";
import type { HubResult, ScanVerdict } from "../../lib/restTypes.ts";
import { errorText, useToast } from "../../shell/Toasts.tsx";
import {
  Badge,
  type BadgeTone,
  Button,
  Card,
  CardHeader,
  Input,
  Notice,
  QueryState,
} from "../../ui/controls.tsx";

const VERDICT_TONE: Record<ScanVerdict, BadgeTone> = {
  safe: "ok",
  caution: "warn",
  dangerous: "danger",
};

function HubRow({ hit }: { hit: HubResult }) {
  const toast = useToast();
  const client = useQueryClient();
  const [problem, setProblem] = useState("");
  const done = (message: string) => {
    setProblem("");
    toast.show(message, "success");
    void client.invalidateQueries({ queryKey: ["skills"] });
  };
  const fail = (error: unknown) => {
    if (error instanceof ApiError && error.status === 409) {
      setProblem(`${error.message} The install was refused.`);
    } else {
      setProblem(errorText(error));
    }
  };
  const install = useMutation({
    mutationFn: () => api.skills.hubInstall(hit.identifier),
    onSuccess: () => done(`${hit.name} installed.`),
    onError: fail,
  });
  const update = useMutation({
    mutationFn: () => api.skills.hubUpdate(hit.name),
    onSuccess: () => done(`${hit.name} updated.`),
    onError: fail,
  });
  const uninstall = useMutation({
    mutationFn: () => api.skills.hubUninstall(hit.name),
    onSuccess: () => done(`${hit.name} removed.`),
    onError: fail,
  });
  const blocked = hit.verdict === "dangerous";
  return (
    <li
      className="flex flex-col gap-2 border border-line px-4 py-3"
      data-testid={`hub-${hit.identifier}`}
    >
      <div className="flex items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="type-data text-[0.875rem] text-ink">{hit.name}</span>
            {hit.verdict === null ? (
              <Badge>scanned on install</Badge>
            ) : (
              <Badge tone={VERDICT_TONE[hit.verdict]}>scan: {hit.verdict}</Badge>
            )}
            <Badge tone="secondary">{hit.source}</Badge>
            {hit.installed ? <Badge tone="ok">installed</Badge> : null}
          </div>
          <p className="m-0 text-[0.75rem] text-ink-muted">{hit.description}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {hit.installed ? (
            <>
              {hit.update_available ? (
                <Button size="sm" onClick={() => update.mutate()} disabled={update.isPending}>
                  Update
                </Button>
              ) : null}
              <Button
                tone="ghost"
                size="sm"
                className="text-danger"
                onClick={() => uninstall.mutate()}
                disabled={uninstall.isPending}
              >
                Uninstall
              </Button>
            </>
          ) : (
            <Button
              tone="primary"
              size="sm"
              onClick={() => install.mutate()}
              disabled={blocked || install.isPending}
            >
              {blocked ? "Blocked by the scanner" : "Install"}
            </Button>
          )}
        </div>
      </div>
      {problem ? <Notice tone="danger">{problem}</Notice> : null}
    </li>
  );
}

export function HubView() {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const results = useQuery({
    queryKey: ["skills", "hub", query],
    queryFn: () => api.skills.hubSearch(query),
    enabled: query.length > 0,
  });
  const list = results.data?.results ?? [];
  return (
    <Card padded={false}>
      <CardHeader
        icon={Search}
        title="Browse hub"
        aside={query && results.data ? <Badge tone="secondary">{list.length} results</Badge> : null}
      />
      <div className="flex flex-col gap-3 p-4">
        <search>
          <form
            className="flex gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              setQuery(draft.trim());
            }}
          >
            <Input
              aria-label="Search the skills hub"
              className="flex-1"
              placeholder="Search the hub..."
              value={draft}
              onChange={(e) => setDraft(e.currentTarget.value)}
            />
            <Button type="submit" icon={<Search />}>
              Search
            </Button>
          </form>
        </search>
        {query ? (
          <QueryState
            loading={results.isPending}
            error={results.error}
            empty={list.length === 0}
            emptyText="Nothing in the hub matches."
          >
            <ul className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Hub results">
              {list.map((hit) => (
                <HubRow key={hit.identifier} hit={hit} />
              ))}
            </ul>
          </QueryState>
        ) : (
          <p className="m-0 text-[0.8125rem] text-ink-muted">
            Search the hub for skills to install. Every result shows the safety scanner's verdict.
          </p>
        )}
      </div>
    </Card>
  );
}
