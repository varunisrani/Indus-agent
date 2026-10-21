// A fake of main's REST proxy for settings and onboarding tests: routes answer from a table and
// every call is recorded (method, path, body, profile).

import type { IndusApiRequest, IndusApiResult } from "../global.d.ts";
import type { RestLike } from "./settingsApi.ts";

export type Route = (request: IndusApiRequest) => { status: number; body: unknown };

export interface FakeRest extends RestLike {
  readonly calls: IndusApiRequest[];
  route(
    method: IndusApiRequest["method"],
    path: string,
    answer: Route | { readonly [key: string]: unknown },
  ): void;
}

export function fakeRest(): FakeRest {
  const routes = new Map<string, Route>();
  const calls: IndusApiRequest[] = [];
  return {
    calls,
    route(method, path, answer) {
      routes.set(
        `${method} ${path}`,
        typeof answer === "function" ? (answer as Route) : () => ({ status: 200, body: answer }),
      );
    },
    async request(request) {
      calls.push(request);
      const route = routes.get(`${request.method} ${request.path}`);
      if (route === undefined) {
        return { kind: "not-found", status: 404, body: { detail: "not found" } };
      }
      const { status, body } = route(request);
      return (
        status === 404
          ? { kind: "not-found", status: 404, body }
          : { kind: "response", status, body }
      ) as IndusApiResult;
    },
  };
}
