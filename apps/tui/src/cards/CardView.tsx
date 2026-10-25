// Renders the overlay store's card by kind (wired into the layout by P12-27).

import type { ReactNode } from "react";
import { ModelPicker } from "../pickers/ModelPicker.tsx";
import type { Card } from "../state/overlayStore.ts";
import { ApprovalCard } from "./ApprovalCard.tsx";
import { ClarifyCard } from "./ClarifyCard.tsx";
import { ConfirmCard } from "./ConfirmCard.tsx";
import { MaskedCard } from "./MaskedCard.tsx";
import { askConfirm } from "./openCards.ts";
import { Pager } from "./Pager.tsx";
import type {
  ApprovalPayload,
  ClarifyPayload,
  ConfirmPayload,
  MaskedPayload,
  ModelPickerPayload,
  PagerPayload,
} from "./payloads.ts";
import type { RpcLike } from "./rpcLike.ts";

export interface CardViewProps {
  card: Card;
  columns: number;
  rows: number;
  rpc: RpcLike;
  /** Close a card that has no answer of its own (pager, model picker). */
  close(id: string): void;
  systemLine(text: string): void;
}

export function CardView({
  card,
  columns,
  rows,
  rpc,
  close,
  systemLine,
}: CardViewProps): ReactNode {
  const done = (): void => close(card.id);
  switch (card.kind) {
    case "approval": {
      const p = card.payload as ApprovalPayload;
      return <ApprovalCard key={card.id} request={p} columns={columns} onChoose={p.choose} />;
    }
    case "confirm": {
      const p = card.payload as ConfirmPayload;
      return <ConfirmCard key={card.id} request={p} onConfirm={p.confirm} onCancel={p.cancel} />;
    }
    case "pager": {
      const p = card.payload as PagerPayload;
      const title = p.title === undefined ? {} : { title: p.title };
      return (
        <Pager
          key={card.id}
          {...title}
          lines={p.lines}
          rows={rows}
          columns={columns}
          onClose={done}
        />
      );
    }
    case "clarify": {
      const p = card.payload as ClarifyPayload;
      return (
        <ClarifyCard key={card.id} questions={p.questions} onLock={p.lock} onCancel={p.cancel} />
      );
    }
    case "sudo":
    case "secret": {
      const p = card.payload as MaskedPayload;
      const detail = p.detail ? { detail: p.detail } : {};
      return <MaskedCard key={card.id} title={p.title} {...detail} onSubmit={p.submit} />;
    }
    case "modelPicker": {
      const p = card.payload as ModelPickerPayload;
      return (
        <ModelPicker
          key={card.id}
          rpc={rpc}
          sessionId={p.sessionId}
          refresh={p.refresh}
          onClose={done}
          systemLine={systemLine}
          confirm={(options) => askConfirm(options)}
        />
      );
    }
    default:
      return null;
  }
}
