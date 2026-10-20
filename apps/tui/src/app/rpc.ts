// The renderer's one door to the bridge: method calls and answers to server questions, injected
// through React context so components and tests can swap it. Backed by `@indus/client`.

import type { IndusClient } from "@indus/client";
import type { ServerRequestAnswer } from "@indus/protocol";
import { createContext, createElement, type ReactNode, useContext } from "react";
import { uiStore } from "../state/uiStore.ts";

export interface Rpc {
  call<M extends string>(method: M, params: unknown): Promise<unknown>;
  /** Answer an open server request (a card's result). */
  answer(requestId: string, answer: unknown): Promise<void>;
  /**
   * Addition to the wave-8 contract: a server-request handler parks here until a card calls
   * `answer` for the same id, so the answer travels back through the channel's handler promise.
   */
  waitForAnswer?(requestId: string, signal?: AbortSignal): Promise<unknown>;
}

interface Waiter {
  resolve: (answer: unknown) => void;
  reject: (error: unknown) => void;
}

/** An `Rpc` over an `IndusClient`. */
export function rpcFromClient(client: IndusClient): Rpc {
  const waiters = new Map<string, Waiter>();
  return {
    call: (method, params) => client.channel.call(method, params as object),
    answer: async (requestId, answer) => {
      const waiter = waiters.get(requestId);
      if (waiter !== undefined) {
        waiters.delete(requestId);
        waiter.resolve(answer);
        return;
      }
      const sessionId = uiStore.getState().sid;
      if (sessionId === null) throw new Error("no session to answer for");
      // The daemon (P17-20) needs the generation of the turn that asked.
      const generation = client.channel.generationOf(requestId);
      await client.request("server_request.answer", {
        session_id: sessionId,
        request_id: requestId,
        answer: answer as ServerRequestAnswer,
        ...(generation === undefined ? {} : { generation }),
      });
    },
    waitForAnswer: (requestId, signal) =>
      new Promise((resolve, reject) => {
        if (signal?.aborted) {
          reject(signal.reason);
          return;
        }
        waiters.set(requestId, { resolve, reject });
        signal?.addEventListener(
          "abort",
          () => {
            if (waiters.get(requestId)?.resolve === resolve) waiters.delete(requestId);
            reject(signal.reason);
          },
          { once: true },
        );
      }),
  };
}

/** Fails every call; the default until the app provides a real one. */
export const offlineRpc: Rpc = {
  call: (method) => Promise.reject(new Error(`not connected (${method})`)),
  answer: (requestId) => Promise.reject(new Error(`not connected (answer ${requestId})`)),
};

const RpcContext = createContext<Rpc>(offlineRpc);

export function RpcProvider(props: { rpc: Rpc; children?: ReactNode }): ReactNode {
  return createElement(RpcContext.Provider, { value: props.rpc }, props.children);
}

export function useRpc(): Rpc {
  return useContext(RpcContext);
}
