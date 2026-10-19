// Model picker card: loads `model.options`, walks provider → model → effort, applies through
// `config.set`. The composer draft is never touched.

import type {
  ModelDisconnectResult,
  ModelOptionsResult,
  ModelSaveKeyResult,
} from "@indus/protocol";
import { Box, Text, useInput } from "ink";
import { type ReactNode, useEffect, useRef, useState } from "react";
import type { ConfirmOptions } from "../cards/openCards.ts";
import { errorText, type RpcLike } from "../cards/rpcLike.ts";
import { useTones } from "../cards/tones.ts";
import {
  applyModelChoice,
  chosenProvider,
  disconnected,
  EFFORT_ROWS,
  initialPicker,
  keyFailed,
  keySaved,
  optionsFailed,
  optionsLoaded,
  type PickerEffect,
  type PickerState,
  pickerStep,
  SecretBuffer,
  VISIBLE_ROWS,
  visibleModels,
  visibleProviders,
  windowOf,
} from "./modelPickerState.ts";

export interface ModelPickerProps {
  rpc: RpcLike;
  sessionId: string | null;
  /** `/model --refresh`: our bridge has no refresh flag, so this only forces a fresh call. */
  refresh?: boolean;
  allowGlobal?: boolean;
  onClose(): void;
  systemLine(text: string): void;
  confirm(options: ConfirmOptions): Promise<boolean>;
  active?: boolean;
}

export function ModelPicker(props: ModelPickerProps): ReactNode {
  const { rpc, sessionId, allowGlobal = false, active = true } = props;
  const tone = useTones();
  const [state, setState] = useState<PickerState>(() => initialPicker(allowGlobal));
  const latest = useRef(state);
  const buffer = useRef(new SecretBuffer());
  const alive = useRef(true);
  const commit = (next: PickerState): void => {
    latest.current = next;
    if (alive.current) setState(next);
  };

  // biome-ignore lint/correctness/useExhaustiveDependencies: load once per session and refresh flag
  useEffect(() => {
    alive.current = true;
    const params = sessionId === null ? {} : { session_id: sessionId };
    rpc.call("model.options", params).then(
      (result) => commit(optionsLoaded(latest.current, result as ModelOptionsResult)),
      (error: unknown) => commit(optionsFailed(latest.current, errorText(error))),
    );
    return () => {
      alive.current = false;
      buffer.current.clear();
    };
  }, [rpc, sessionId, props.refresh]);

  const run = (effect: PickerEffect): void => {
    if (effect === null) return;
    if (effect.type === "close") {
      props.onClose();
      return;
    }
    if (effect.type === "apply") {
      void applyModelChoice(
        rpc,
        { sessionId, provider: effect.provider, model: effect.model, effort: effect.effort },
        { confirm: props.confirm, systemLine: props.systemLine },
      ).then(() => props.onClose());
      return;
    }
    if (effect.type === "saveKey") {
      const apiKey = buffer.current.take();
      rpc.call("model.save_key", { slug: effect.slug, api_key: apiKey }).then(
        (result) => commit(keySaved(latest.current, (result as ModelSaveKeyResult).provider)),
        (error: unknown) => commit(keyFailed(latest.current, errorText(error))),
      );
      return;
    }
    rpc.call("model.disconnect", { slug: effect.slug }).then(
      (result) =>
        commit(
          disconnected(latest.current, effect.slug, (result as ModelDisconnectResult).removed),
        ),
      () => commit(disconnected(latest.current, effect.slug, false)),
    );
  };

  useInput(
    (input, key) => {
      const { state: next, effect } = pickerStep(latest.current, input, key, buffer.current);
      commit(next);
      run(effect);
    },
    { isActive: active },
  );

  return (
    <Box flexDirection="column" borderStyle="round" borderColor={tone.accent} paddingX={1}>
      <PickerBody state={state} />
      {state.note ? <Text color={tone.warn}>{state.note}</Text> : null}
      <Text color={tone.muted}>
        {footerFor(state)} · scope: {state.scope === "global" ? "global" : "session only"}
        {state.allowGlobal ? " (Ctrl+G switches)" : ""}
      </Text>
    </Box>
  );
}

function footerFor(state: PickerState): string {
  switch (state.stage) {
    case "provider":
      return "type to filter · Enter choose · Ctrl+K key · Ctrl+D sign out · Esc close";
    case "model":
      return "type to filter (or a model id) · Enter choose · Esc back";
    case "effort":
      return "↑↓ or 1-7 · Enter apply · Esc back";
    case "key":
      return "paste the key · Enter save · Esc back";
    case "disconnect":
      return "y/Enter sign out · n/Esc back";
  }
}

function PickerBody({ state }: { state: PickerState }): ReactNode {
  const tone = useTones();
  if (state.loading) return <Text color={tone.muted}>Loading providers…</Text>;
  if (state.error !== null) {
    return <Text color={tone.error}>Could not list providers: {state.error}</Text>;
  }
  const provider = chosenProvider(state);
  const current = state.currentModel ? `${state.currentModel}` : "unknown";

  if (state.stage === "key" && provider) {
    return (
      <Box flexDirection="column">
        <Text bold color={tone.accent}>
          API key for {provider.name}
        </Text>
        <Text color={tone.text}>
          {"> "}
          {"•".repeat(Math.min(state.keyLength, 32))}
          {state.working ? " saving…" : "▏"}
        </Text>
      </Box>
    );
  }
  if (state.stage === "disconnect" && provider) {
    return (
      <Box flexDirection="column">
        <Text bold color={tone.warn}>
          Sign out of {provider.name}?
        </Text>
        <Text color={tone.text}>The stored key is removed; you can add it again later.</Text>
        {state.working ? <Text color={tone.muted}>signing out…</Text> : null}
      </Box>
    );
  }
  if (state.stage === "effort") {
    return (
      <Box flexDirection="column">
        <Text bold color={tone.accent}>
          Reasoning effort for {state.pendingModel}
        </Text>
        {EFFORT_ROWS.map((row, i) => (
          <Text key={row.label} color={i === state.effortIndex ? tone.accent : tone.muted}>
            {i === state.effortIndex ? "› " : "  "}
            {i + 1}. {row.label}
          </Text>
        ))}
        {state.working ? <Text color={tone.muted}>switching…</Text> : null}
      </Box>
    );
  }

  const listing = state.stage === "model";
  const labels = listing
    ? visibleModels(state).map((model) =>
        model === state.currentModel && provider?.slug === state.currentProvider
          ? `${model} (current)`
          : model,
      )
    : visibleProviders(state).map(
        (row) =>
          `${row.authenticated ? "●" : "○"} ${row.name}${row.current ? " (current)" : ""}${row.authenticated ? "" : row.authType === "api_key" ? " · no key" : " · needs sign-in"}`,
      );
  const selected = listing ? state.modelIndex : state.providerIndex;
  const view = windowOf(labels.length, selected, VISIBLE_ROWS);
  const empty = listing
    ? state.filter.trim()
      ? `no listed model matches; Enter uses "${state.filter.trim()}"`
      : "this provider lists no models: type a model id"
    : "no provider matches";

  return (
    <Box flexDirection="column">
      <Text bold color={tone.accent}>
        {listing ? `Models of ${provider?.name ?? "?"}` : "Choose a provider"}
      </Text>
      <Text color={tone.muted}>now: {current}</Text>
      <Text color={state.filter ? tone.accent : tone.muted}>
        {state.filter ? `filter: ${state.filter}▏` : "type to filter"}
      </Text>
      {view.start > 0 ? <Text color={tone.muted}> ↑ {view.start} more</Text> : null}
      {labels.length === 0 ? <Text color={tone.muted}>{empty}</Text> : null}
      {labels.slice(view.start, view.end).map((label, i) => {
        const index = view.start + i;
        return (
          <Text
            key={`${index}:${label}`}
            color={index === selected ? tone.accent : tone.text}
            wrap="truncate-end"
          >
            {index === selected ? "› " : "  "}
            {label}
          </Text>
        );
      })}
      {view.end < labels.length ? (
        <Text color={tone.muted}> ↓ {labels.length - view.end} more</Text>
      ) : null}
    </Box>
  );
}
