// @vitest-environment jsdom
import { type OutboundFrame, RpcChannel, type Transport } from "@indus/client";
import type { ApprovalRequest, ServerRequestAnswer } from "@indus/protocol";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ServerRequestStoreProvider } from "../../gateway/react.tsx";
import { type OpenServerRequest, ServerRequestStore } from "../../gateway/serverRequests.ts";
import { FakeConnection } from "../testFakes.ts";
import { CARD_KINDS, CardStack } from "./CardStack.tsx";

afterEach(cleanup);

function open(
  kind: OpenServerRequest["kind"],
  request: Record<string, unknown>,
  answers: ServerRequestAnswer[],
): OpenServerRequest {
  let done = false;
  return {
    id: String(request.request_id),
    sessionId: "s1",
    kind,
    request: { session_id: "s1", kind, ...request } as never,
    replayed: false,
    receivedAt: 0,
    answer: (answer) => {
      if (done) return false;
      done = true;
      answers.push(answer);
      return true;
    },
  };
}

const approval = (answers: ServerRequestAnswer[], extra: Partial<ApprovalRequest> = {}) =>
  open(
    "approval",
    {
      request_id: "r1",
      command: "rm -rf build/",
      description: "Deletes a folder",
      choices: ["once", "session", "deny"],
      ...extra,
    },
    answers,
  );

function Transcript(props: { tools: string[]; requests: readonly OpenServerRequest[] }) {
  return (
    <div>
      <ol>
        {props.tools.map((tool) => (
          <li key={tool} data-tool={tool}>
            {tool}
          </li>
        ))}
      </ol>
      <CardStack sessionId="s1" requests={props.requests} connection={null} armMs={0} />
    </div>
  );
}

describe("card stack", () => {
  it("keeps the same card instance when the tool row appears after the approval", () => {
    const answers: ServerRequestAnswer[] = [];
    const requests = [approval(answers)];
    const view = render(<Transcript tools={[]} requests={requests} />);
    const before = view.container.querySelector('[data-request-id="r1"]');
    const input = screen.getByLabelText("Reason for denying (optional)") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "half typed" } });
    view.rerender(<Transcript tools={["terminal"]} requests={requests} />);
    const after = view.container.querySelector('[data-request-id="r1"]');
    expect(after).toBe(before);
    expect((screen.getByLabelText("Reason for denying (optional)") as HTMLInputElement).value).toBe(
      "half typed",
    );
  });

  it("shows exactly the backend's choices, deny always among them", () => {
    render(<Transcript tools={[]} requests={[approval([])]} />);
    const choices = [...document.querySelectorAll("[data-choice]")].map((b) =>
      b.getAttribute("data-choice"),
    );
    expect(choices).toEqual(["once", "session", "deny"]);
    cleanup();
    render(<Transcript tools={[]} requests={[approval([], { choices: ["always"] })]} />);
    const again = [...document.querySelectorAll("[data-choice]")].map((b) =>
      b.getAttribute("data-choice"),
    );
    expect(again).toEqual(["always", "deny"]);
    cleanup();
    render(<Transcript tools={[]} requests={[approval([], { choices: [] })]} />);
    const unnamed = [...document.querySelectorAll("[data-choice]")].map((b) =>
      b.getAttribute("data-choice"),
    );
    expect(unnamed).toEqual(["once", "session", "deny"]);
  });

  it("a drag over Allow does not answer", () => {
    const answers: ServerRequestAnswer[] = [];
    render(<Transcript tools={[]} requests={[approval(answers)]} />);
    const allow = screen.getByRole("button", { name: "Allow once" });
    const command = screen.getByText("rm -rf build/");
    // press started on the command text, released over the button
    fireEvent.pointerDown(command, { button: 0 });
    fireEvent.pointerUp(allow, { button: 0 });
    fireEvent.click(allow, { detail: 1 });
    expect(answers).toEqual([]);
  });

  it("a real click or the keyboard answers, once", () => {
    const answers: ServerRequestAnswer[] = [];
    render(<Transcript tools={[]} requests={[approval(answers)]} />);
    const allow = screen.getByRole("button", { name: "Allow for this session" });
    fireEvent.pointerDown(allow, { button: 0 });
    fireEvent.click(allow, { detail: 1 });
    expect(answers).toEqual([{ kind: "approval", choice: "session" }]);
    // a second answer (a replayed card clicked again) changes nothing
    fireEvent.click(screen.getByRole("button", { name: "Deny" }), { detail: 0 });
    expect(answers).toHaveLength(1);

    const keyed: ServerRequestAnswer[] = [];
    cleanup();
    render(<Transcript tools={[]} requests={[approval(keyed)]} />);
    const deny = screen.getByRole("button", { name: "Deny" });
    fireEvent.change(screen.getByLabelText("Reason for denying (optional)"), {
      target: { value: "not now" },
    });
    fireEvent.click(deny, { detail: 0 }); // keyboard activation
    expect(keyed).toEqual([{ kind: "approval", choice: "deny", reason: "not now" }]);
  });

  it("ignores clicks before the card is armed", async () => {
    const answers: ServerRequestAnswer[] = [];
    render(
      <CardStack sessionId="s1" requests={[approval(answers)]} connection={null} armMs={50} />,
    );
    const allow = screen.getByRole("button", { name: "Allow once" });
    fireEvent.pointerDown(allow, { button: 0 });
    fireEvent.click(allow, { detail: 1 });
    expect(answers).toEqual([]);
    await act(() => new Promise((resolve) => setTimeout(resolve, 80)));
    fireEvent.pointerDown(allow, { button: 0 });
    fireEvent.click(allow, { detail: 1 });
    expect(answers).toHaveLength(1);
  });

  it("clarify: one card, each question locked on its own", async () => {
    const answers: ServerRequestAnswer[] = [];
    const connection = new FakeConnection();
    let remaining = ["q1", "q2"];
    connection.on("clarify.lock", (params) => {
      remaining = remaining.filter((id) => id !== params.question_id);
      return { remaining };
    });
    const request = open(
      "clarify",
      {
        request_id: "c1",
        questions: [
          { id: "q1", text: "Which branch?", choices: ["main", "dev"] },
          { id: "q2", text: "Anything else?" },
        ],
      },
      answers,
    );
    render(<CardStack sessionId="s1" requests={[request]} connection={connection} armMs={0} />);
    expect(document.querySelectorAll('[data-card="clarify"]')).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "dev" }));
    const [first] = screen.getAllByRole("button", { name: "Answer" });
    fireEvent.pointerDown(first as HTMLElement, { button: 0 });
    await act(async () => {
      fireEvent.click(first as HTMLElement, { detail: 1 });
    });
    expect(connection.called("clarify.lock")[0]?.params).toEqual({
      session_id: "s1",
      request_id: "c1",
      question_id: "q1",
      answer: ["dev"],
    });
    expect(document.querySelector('[data-question="q1"]')?.getAttribute("data-locked")).toBe(
      "true",
    );
    const skip = screen.getByRole("button", { name: "Skip" });
    fireEvent.pointerDown(skip, { button: 0 });
    await act(async () => {
      fireEvent.click(skip, { detail: 1 });
    });
    expect(connection.called("clarify.lock")[1]?.params.answer).toEqual([]);
    expect(remaining).toEqual([]);
    // the locks answered it; nothing goes through the generic answer
    expect(answers).toEqual([]);
  });

  it("sudo dialog: full selectable reason, unblurred backdrop, password only in the answer", () => {
    const answers: ServerRequestAnswer[] = [];
    const reason = "sudo apt-get install --yes build-essential pkg-config libssl-dev";
    render(
      <CardStack
        sessionId="s1"
        requests={[open("sudo", { request_id: "u1", reason }, answers)]}
        connection={null}
      />,
    );
    expect(screen.getByTestId("sudo-reason").textContent).toBe(reason);
    expect(screen.getByTestId("sudo-reason").className).toContain("select-text");
    const backdrop = document.querySelector("[data-backdrop]") as HTMLElement;
    expect(backdrop.className).not.toMatch(/blur/);
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(answers).toEqual([{ kind: "sudo", password: "pw" }]);
  });

  it("secret dialog writes only through the answer, never to storage", () => {
    const answers: ServerRequestAnswer[] = [];
    localStorage.clear();
    sessionStorage.clear();
    render(
      <CardStack
        sessionId="s1"
        requests={[
          open("secret", { request_id: "k1", name: "SERVICE_TOKEN", prompt: "Paste it" }, answers),
        ]}
        connection={null}
      />,
    );
    const value = ["v", "a", "l", "u", "e"].join("");
    fireEvent.change(screen.getByLabelText("Value for SERVICE_TOKEN"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(answers).toEqual([{ kind: "secret", value }]);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});

/** A transport whose frames the test reads and whose inbound side the test drives. */
class TestTransport implements Transport {
  readonly sent: OutboundFrame[] = [];
  private onMessage: ((frame: unknown) => void) | null = null;
  send(frame: OutboundFrame): void {
    this.sent.push(frame);
  }
  close(): void {}
  onFrame(listener: (frame: unknown) => void) {
    this.onMessage = listener;
    return () => {
      this.onMessage = null;
    };
  }
  onClose() {
    return () => {};
  }
  deliver(frame: unknown) {
    this.onMessage?.(frame);
  }
}

describe("server requests through the client channel", () => {
  it("handles exactly the four card kinds", () => {
    expect(Object.keys(new ServerRequestStore().handlers()).sort()).toEqual([...CARD_KINDS].sort());
  });

  it("an unknown kind is refused with -32601 at once", async () => {
    const store = new ServerRequestStore();
    const transport = new TestTransport();
    const channel = new RpcChannel();
    channel.setHandlers({ serverRequests: store.handlers() });
    channel.attach(transport as never);
    transport.deliver({
      jsonrpc: "2.0",
      method: "server_request",
      params: {
        request: { request_id: "p1", session_id: "s1", kind: "preview.act" },
        replayed: false,
        generation: 2,
      },
    });
    await Promise.resolve();
    expect(transport.sent).toContainEqual(
      expect.objectContaining({ id: "p1", error: expect.objectContaining({ code: -32601 }) }),
    );
    expect(store.list("s1")).toEqual([]);
  });

  it("a request settled from another surface closes its card", async () => {
    const store = new ServerRequestStore();
    const transport = new TestTransport();
    const channel = new RpcChannel();
    channel.setHandlers({ serverRequests: store.handlers() });
    channel.attach(transport as never);
    render(
      <ServerRequestStoreProvider value={store}>
        <CardStack sessionId="s1" connection={null} armMs={0} />
      </ServerRequestStoreProvider>,
    );
    await act(async () => {
      transport.deliver({
        jsonrpc: "2.0",
        method: "server_request",
        params: {
          request: {
            request_id: "r9",
            session_id: "s1",
            kind: "approval",
            command: "make deploy",
            description: "Deploys",
            choices: ["once", "deny"],
          },
          replayed: false,
          generation: 5,
        },
      });
    });
    expect(screen.getByText("make deploy")).toBeTruthy();
    await act(async () => {
      transport.deliver({
        jsonrpc: "2.0",
        method: "request.settled",
        params: {
          type: "request.settled",
          session_id: "s1",
          seq: 1,
          request_id: "r9",
          kind: "approval",
          outcome: "answered",
        },
      });
    });
    expect(screen.queryByText("make deploy")).toBeNull();
  });
});
