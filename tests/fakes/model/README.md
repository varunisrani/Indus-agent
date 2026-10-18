# Fake model scripts

`FakeModelServer` (`server.py`) is a model API inside the test process. It answers over
`httpx.MockTransport`, so no socket is ever opened, and it speaks two wire formats from the same
script:

| Request | Answer |
|---|---|
| `POST …/chat/completions` | Chat Completions: SSE chunks, or one JSON object when `"stream": false` |
| `POST …/messages` | Anthropic Messages: SSE events, or one `message` object when `"stream": false` |
| `GET …/models` | `{"object": "list", "data": [...]}` (the `models=` argument, or the server's model) |

Each POST uses up the next turn of the script. A request after the last turn raises
`FakeScriptExhausted`, naming the script, so a test that calls the model once too often fails
loudly.

```python
from tests.fakes.model import FakeModelServer, fake_route, load_script

server = FakeModelServer(load_script("tool_then_text"), clock=fake_clock)
async with server.client() as client:  # or httpx.AsyncClient(transport=server.transport)
    ...
server.model_requests[0].body["messages"]  # what the code under test sent
```

`fake_route(server, "chat_completions" | "anthropic_messages")` builds a route with a fake key
on the `fake.invalid` host. `one_off_router()` gives a respx router for single canned answers.

## File format

Scripts live in `tests/fixtures/model_scripts/` as YAML (a small subset: block mappings and
lists, one-line `[a, b]` / `{k: v}`) or JSON. A bare name (`load_script("hello")`) means
`tests/fixtures/model_scripts/hello.yaml`.

```yaml
turns:                      # one entry per model request
  - steps:                  # played in order
      - reasoning: ["thinking ", "about it"]
      - text: ["Hel", "lo"]
      - tool_call: {name: read_file, arguments: {path: a.txt}, fragments: 3}
      - usage: {prompt: 120, completion: 9, cached: 64}
      - finish: tool_calls
```

Each step is a mapping with exactly one key, its kind:

| Kind | Value | What the server sends |
|---|---|---|
| `text` | string or list of chunks | one content delta per chunk |
| `reasoning` | string or list of chunks | reasoning deltas (Anthropic: a thinking block, closed with a fake signature) |
| `tool_call` | `name`, `arguments` (mapping, or a raw string for broken JSON), `id`, `fragments` (default 3), `late_id` | the call, its arguments split into that many pieces; with `late_id` the id comes with the second piece (Chat Completions only) |
| `usage` | `prompt`, `completion`, `cached` | Chat Completions: a usage chunk at this point; Anthropic: input usage in `message_start`, output usage in `message_delta` |
| `keep_alive` | frame count (default 1) | SSE comment lines / `ping` events, no content |
| `mid_stream_error` | mapping or string | an error frame inside the open stream (a string is sent as plain-text data), then the stream ends |
| `stall` | seconds | waits on the server's clock (pass `clock=`); nothing is sent meanwhile |
| `disconnect` | nothing | the body ends abruptly (`httpx.RemoteProtocolError` on the client) |
| `eof` | nothing | the body ends cleanly here, without a finish or end-of-stream marker |
| `fail` | `status`, `body`, `headers` | an HTTP error answer instead of a stream; only as a turn's first step |
| `finish` | reason (`stop`, `tool_calls`, `length`, `content_filter`, or any raw word) | the finish chunk and `[DONE]` / `message_delta` + `message_stop` |

A turn that runs out of steps without `finish` gets an implied one (`tool_calls` when it called a
tool, else `stop`) and the end-of-stream marker. To script a stream that stops early, end the turn
with `eof` (clean end) or `disconnect` (cut).

## What a test can look at

- `server.requests` / `server.model_requests`: `RecordedRequest(method, path, headers, body,
  turn, at, timeout)` — `body` is the parsed JSON, `at` the server clock time, `timeout` the
  httpx timeout the client sent.
- `server.streams`: one `StreamRecord` per streamed answer with `opened_at`, `closed_at`
  (`time.perf_counter()` seconds), `frames_sent` and `completed`.
- `server.turns_left`: turns not used yet.
- `yield_every=N` gives way to the event loop after every N frames and `on_frame(turn, bytes)`
  sees each frame as it is sent (latency measurements in `tests/trials/`).
