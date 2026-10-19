# Third-party notices

## Design reference

Indus Agent's design is inspired by **hermes-agent** by Nous Research
(<https://github.com/NousResearch/hermes-agent>), released under the MIT License. We studied its
behaviour at commit `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`.

Indus Agent is written from scratch. No source code, comments, documentation, tests or assets of
hermes-agent are included in this repository. The clean-room guards in `tooling/checks/` (similarity,
banned identifiers, asset hashes) check every change against the pinned reference, in CI and before
each release; the protocol is described in `docs/plan/01-clean-room-protocol.md`.

We include the reference's licence below as credit:

```text
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Dependencies

Notices for the third-party packages Indus Agent ships with will be generated from the installed
dependencies at build time and listed here (P25). Until then this table lists each runtime
dependency and what it pulls in, with its licence from the package metadata (installed, or on
PyPI for packages of other platforms).

| Package | Version | Licence | Why | Added by |
|---|---|---|---|---|
| keyring | 25.7.0 | MIT | OS keychain access for `SecretStore` | P01-16 |
| jaraco.classes | 3.4.0 | MIT | via keyring | P01-16 |
| jaraco.context | 6.1.2 | MIT | via keyring | P01-16 |
| jaraco.functools | 4.6.0 | MIT | via keyring | P01-16 |
| more-itertools | 11.1.0 | MIT | via jaraco.classes / jaraco.functools | P01-16 |
| SecretStorage | 3.5.0 | BSD-3-Clause | via keyring (Linux) | P01-16 |
| jeepney | 0.9.0 | MIT | via keyring / SecretStorage (Linux) | P01-16 |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | via SecretStorage (Linux) | P01-16 |
| cffi | 2.1.1 | MIT-0 (exception recorded in `tooling/checks/licenses_allow.json`) | via cryptography | P01-16 |
| pycparser | 3.0 | BSD-3-Clause | via cffi | P01-16 |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | via keyring (Windows) | P01-16 |
| pydantic | 2.13.5 | MIT | config schema and defaults | P01-09 |
| pydantic-core | 2.46.5 | MIT | via pydantic | P01-09 |
| annotated-types | 0.8.0 | MIT | via pydantic | P01-09 |
| typing-extensions | 4.16.0 | PSF-2.0 | via pydantic / pydantic-core / typing-inspection (and python-docx in extra `docs`) | P01-09 |
| typing-inspection | 0.4.4 | MIT | via pydantic | P01-09 |
| ruamel.yaml | 0.19.1 | MIT | YAML 1.1 load and round-trip dump (`yamlio`) | P01-08 |
| httpx | 0.28.1 | BSD-3-Clause | async HTTP client for model providers | P03-08 |
| httpcore | 1.0.9 | BSD-3-Clause | via httpx | P03-08 |
| h11 | 0.16.0 | MIT | via httpcore | P03-08 |
| anyio | 4.15.1 | MIT | via httpx (pinned directly) | P03-08 |
| certifi | 2026.7.22 | MPL-2.0 (unmodified) | via httpx (CA bundle) | P03-08 |
| idna | 3.20 | BSD-3-Clause | via httpx / anyio | P03-08 |
| openpyxl | 3.1.5 | MIT | extra `docs`: `.xlsx` text for `read_file` | P05-17 |
| et-xmlfile | 2.0.0 | MIT | extra `docs`, via openpyxl | P05-17 |
| pypdf | 6.19.0 | BSD-3-Clause | extra `docs`: PDF text layer for `read_file` | P05-17 |
| python-docx | 1.2.0 | MIT | extra `docs`: `.docx` text for `read_file` | P05-17 |
| lxml | 6.1.3 | BSD-3-Clause | extra `docs`, via python-docx; extra `web`, via ddgs | P05-17 |
| ptyprocess | 0.7.0 | ISC | extra `pty`: pseudo-terminals for interactive background processes (also via pexpect, dev only) | P05-13 |
| ddgs | 9.16.0 | MIT | extra `web`: keyless web search | P05-23 |
| primp | 2.0.1 | MIT | extra `web`, via ddgs | P05-23 |
| playwright | 1.63.0 | Apache-2.0 | extra `browser`: drives Chromium for the browser tools (the wheel carries the Playwright driver and its Node.js runtime; Chromium itself is installed separately, never shipped) | P10-12 |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | extra `browser`, via playwright | P10-12 |
| pyee | 13.0.1 | MIT | extra `browser`, via playwright | P10-12 |
| faster-whisper | 1.2.1 | MIT | extra `voice`: local speech to text for the STT library | P10-18 |
| av | 19.0.1 | BSD-3-Clause (the wheels bundle FFmpeg libraries under LGPL-2.1-or-later; owner check open, see LEAD-NOTES) | extra `voice`, via faster-whisper (audio decoding) | P10-18 |
| ctranslate2 | 4.8.2 | MIT | extra `voice`, via faster-whisper (inference engine) | P10-18 |
| huggingface-hub | 1.33.0 | Apache-2.0 | extras `voice`, `research`, via faster-whisper / tokenizers (model download) | P10-18 |
| hf-xet | 1.6.0 | Apache-2.0 | extras `voice`, `research`, via huggingface-hub | P10-18 |
| filelock | 4.0.10 | MIT | extras `voice`, `research`, via huggingface-hub | P10-18 |
| fsspec | 2026.6.0 | BSD-3-Clause | extras `voice`, `research`, via huggingface-hub / datasets (datasets 5.0.1 caps it at 2026.6.0) | P10-18 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT (unmodified) | extras `voice`, `research`, via faster-whisper / huggingface-hub | P10-18 |
| pyyaml | 6.0.3 | MIT | extras `voice`, `research`, via ctranslate2 / huggingface-hub | P10-18 |
| onnxruntime | 1.30.0 | MIT | extra `voice`, via faster-whisper (voice activity filter) | P10-18 |
| flatbuffers | 25.12.19 | Apache-2.0 | extra `voice`, via onnxruntime | P10-18 |
| protobuf | 7.36.2 | BSD-3-Clause | extra `voice`, via onnxruntime | P10-18 |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (Zlib is not on the allow-list; owner check open, see LEAD-NOTES) | extras `voice`, `research`, via ctranslate2 / onnxruntime | P10-18 |
| tokenizers | 0.23.2 | Apache-2.0 | extras `voice`, `research`, via faster-whisper | P10-18 |
| sounddevice | 0.5.6 | MIT (the macOS and Windows wheels bundle PortAudio, MIT) | extra `voice`: microphone and speaker for CLI voice mode; needs cffi | P10-19 |
| prompt_toolkit | 3.0.53 | BSD-3-Clause | the classic REPL's prompt, keys and status bar | P06-04 |
| wcwidth | 0.9.1 | MIT | via prompt_toolkit | P06-04 |
| jsonschema | 4.26.0 | MIT | checks subagent answers against `output_schema` | P10-04 |
| attrs | 26.1.0 | MIT | via jsonschema / referencing | P10-04 |
| jsonschema-specifications | 2025.9.1 | MIT | via jsonschema (the bundled meta-schemas) | P10-04 |
| referencing | 0.37.0 | MIT | via jsonschema | P10-04 |
| rpds-py | 2026.6.3 | MIT | via jsonschema / referencing | P10-04 |
| rich | 15.0.0 | MIT | the classic REPL's streamed output, banner and cards | P06-05 |
| markdown-it-py | 4.2.0 | MIT | via rich | P06-05 |
| mdurl | 0.1.2 | MIT | via markdown-it-py | P06-05 |
| Pygments | 2.21.0 | BSD-2-Clause | via rich | P06-05 |
| click | 8.5.0 | BSD-3-Clause | the `indus` command line: commands, global flags, help (also via ddgs in extra `web`) | P06-01 |
| pexpect | 4.9.0 | ISC | dev only, never shipped: drives `indus` in a pseudo-terminal for the Demo 1 end-to-end tests | P06-14 |
| mcp | 2.3.0 | MIT | the official MCP Python SDK: the MCP client (stdio, Streamable HTTP and SSE transports, sessions) | P13-03 |
| mcp-types | 2.3.0 | MIT | via mcp (protocol message models) | P13-03 |
| httpx2 | 2.13.1 | BSD-3-Clause | via mcp (the SDK's HTTP client; the MCP HTTP transports use it) | P13-03 |
| httpcore2 | 2.13.1 | BSD-3-Clause | via httpx2 | P13-03 |
| httpx2-jsfetch | 1.0 | BSD-3-Clause | via httpx2, Emscripten only (never installed elsewhere) | P13-03 |
| truststore | 0.10.4 | MIT | via httpx2 / httpcore2 (the OS trust store for TLS) | P13-03 |
| pyjwt | 2.15.1 | MIT | via mcp (OAuth helpers; `crypto` extra uses cryptography) | P13-03 |
| opentelemetry-api | 1.45.0 | Apache-2.0 | via mcp (tracing hooks; an exporter comes only with the `daytona` extra) | P13-03 |
| python-multipart | 0.0.32 | Apache-2.0 | via mcp (server side; unused by the client) | P13-03 |
| sse-starlette | 3.5.0 | BSD-3-Clause | via mcp (server side; the fake MCP servers of the tests use it) | P13-03 |
| starlette | 1.7.0 | BSD-3-Clause | via mcp / sse-starlette (server side) | P13-03 |
| uvicorn | 0.54.0 | BSD-3-Clause | serves the gateway's API server and webhook receiver (also via mcp) | P13-03, P15-30 |
| pywin32 | 312 | PSF-2.0 | via mcp, Windows only (process job objects for stdio servers) | P13-03 |
| croniter | 6.2.4 | MIT | five-field cron expressions for scheduled jobs (`indus cron`) | P14-01 |
| python-dateutil | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause (dual licence) | via croniter | P14-01 |
| six | 1.17.0 | MIT | via python-dateutil | P14-01 |
| fastapi | 0.142.2 | MIT | the gateway's HTTP apps: the OpenAI-compatible API server and the webhook receiver (on starlette) | P15-30 |
| annotated-doc | 0.0.5 | MIT | via fastapi; extra `research`, via typer | P15-30, P22-11 |
| modal | 1.6.1 | Apache-2.0 | extra `modal`: the Modal sandbox SDK (terminal backend `modal`) | P21-05 |
| synchronicity | 0.12.6 | Apache-2.0 | extra `modal`, via modal (sync wrappers of its async API) | P21-05 |
| grpclib | 0.4.9 | BSD-3-Clause | extra `modal`, via modal (gRPC client) | P21-05 |
| h2 | 4.4.1 | MIT | extra `modal`, via grpclib (HTTP/2) | P21-05 |
| hpack | 4.2.0 | MIT | extra `modal`, via h2 | P21-05 |
| hyperframe | 6.1.0 | MIT | extra `modal`, via h2 | P21-05 |
| cbor2 | 6.1.5 | MIT | extra `modal`, via modal | P21-05 |
| watchfiles | 1.3.0 | MIT | extra `modal`, via modal (file watching; unused by the backend) | P21-05 |
| toml | 0.10.2 | MIT | extras `modal`, `daytona`, via modal / daytona | P21-05 |
| types-certifi | 2021.10.8.3 | Apache-2.0 | extra `modal`, via modal (type stubs) | P21-05 |
| types-toml | 0.10.8.20260518 | Apache-2.0 | extra `modal`, via modal (type stubs) | P21-05 |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | extras `modal`, `daytona`, `research`, via modal / daytona (HTTP client) | P21-05 |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | extras `modal`, `daytona`, `research`, via aiohttp | P21-05 |
| aiosignal | 1.4.0 | Apache-2.0 | extras `modal`, `daytona`, `research`, via aiohttp | P21-05 |
| frozenlist | 1.8.0 | Apache-2.0 | extras `modal`, `daytona`, `research`, via aiohttp | P21-05 |
| multidict | 6.9.1 | Apache-2.0 | extras `modal`, `daytona`, `research`, via aiohttp / yarl | P21-05 |
| propcache | 0.5.4 | Apache-2.0 | extras `modal`, `daytona`, `research`, via aiohttp / yarl | P21-05 |
| yarl | 1.25.1 | Apache-2.0 | extras `modal`, `daytona`, `research`, via aiohttp | P21-05 |
| daytona | 0.220.0 | Apache-2.0 | extra `daytona`: the Daytona sandbox SDK (terminal backend `daytona`) | P21-07 |
| daytona-api-client | 0.220.0 | Apache-2.0 | extra `daytona`, via daytona (generated API client) | P21-07 |
| daytona-api-client-async | 0.220.0 | Apache-2.0 | extra `daytona`, via daytona | P21-07 |
| daytona-toolbox-api-client | 0.220.0 | Apache-2.0 | extra `daytona`, via daytona | P21-07 |
| daytona-toolbox-api-client-async | 0.220.0 | Apache-2.0 | extra `daytona`, via daytona | P21-07 |
| daytona-analytics-api-client | 0.220.0 | Apache-2.0 (LICENSE file; no metadata field) | extra `daytona`, via daytona | P21-07 |
| daytona-analytics-api-client-async | 0.220.0 | Apache-2.0 (LICENSE file; no metadata field) | extra `daytona`, via daytona | P21-07 |
| aiofiles | 25.1.0 | Apache-2.0 | extra `daytona`, via daytona | P21-07 |
| aiohttp-retry | 2.9.1 | MIT | extra `daytona`, via daytona | P21-07 |
| bidict | 0.24.1 | MPL-2.0 (unmodified) | extra `daytona`, via python-socketio | P21-07 |
| deprecated | 1.3.1 | MIT | extra `daytona`, via daytona / opentelemetry | P21-07 |
| wrapt | 2.5.0 | BSD-2-Clause | extra `daytona`, via deprecated / opentelemetry-instrumentation | P21-07 |
| googleapis-common-protos | 1.75.5 | Apache-2.0 | extra `daytona`, via opentelemetry-exporter-otlp-proto-http | P21-07 |
| httpx-ws | 0.9.0 | MIT | extra `daytona`, via daytona (WebSocket over httpx) | P21-07 |
| wsproto | 1.3.2 | MIT | extra `daytona`, via httpx-ws / simple-websocket | P21-07 |
| obstore | 0.11.1 | MIT | extra `daytona`, via daytona (object storage client) | P21-07 |
| opentelemetry-sdk | 1.45.0 | Apache-2.0 | extra `daytona`, via daytona (tracing; off unless DAYTONA_OTEL_ENABLED) | P21-07 |
| opentelemetry-semantic-conventions | 0.66b0 | Apache-2.0 | extra `daytona`, via opentelemetry-sdk | P21-07 |
| opentelemetry-proto | 1.45.0 | Apache-2.0 | extra `daytona`, via the OTLP exporter | P21-07 |
| opentelemetry-exporter-otlp-proto-http | 1.45.0 | Apache-2.0 | extra `daytona`, via daytona (OTLP exporter; off by default) | P21-07 |
| opentelemetry-exporter-otlp-proto-common | 1.45.0 | Apache-2.0 | extra `daytona`, via the OTLP exporter | P21-07 |
| opentelemetry-exporter-otlp-common | 0.66b0 | Apache-2.0 | extra `daytona`, via the OTLP exporter | P21-07 |
| opentelemetry-exporter-http-transport | 0.66b0 | Apache-2.0 | extra `daytona`, via the OTLP exporter | P21-07 |
| opentelemetry-instrumentation | 0.66b0 | Apache-2.0 | extra `daytona`, via daytona | P21-07 |
| opentelemetry-instrumentation-aiohttp-client | 0.66b0 | Apache-2.0 | extra `daytona`, via daytona | P21-07 |
| opentelemetry-util-http | 0.66b0 | Apache-2.0 | extra `daytona`, via the aiohttp instrumentation | P21-07 |
| python-dotenv | 1.2.4 | BSD-3-Clause | extra `daytona`, via daytona | P21-07 |
| python-socketio | 5.17.0 | MIT | extra `daytona`, via daytona (with its `client` extra) | P21-07 |
| python-engineio | 4.14.0 | MIT | extra `daytona`, via python-socketio | P21-07 |
| simple-websocket | 1.1.0 | MIT | extra `daytona`, via python-engineio | P21-07 |
| requests | 2.34.2 | Apache-2.0 | extras `daytona`, `research`, via python-socketio[client] | P21-07 |
| charset-normalizer | 3.5.2 | MIT | extras `daytona`, `research`, via requests | P21-07 |
| urllib3 | 2.8.0 | MIT | extras `daytona`, `research`, via requests / the generated API clients | P21-07 |
| websocket-client | 1.9.2 | Apache-2.0 | extra `daytona`, via python-socketio[client] | P21-07 |
| vercel-sandbox | 0.7.0 | MIT | extra `vercel`: the Vercel Sandbox SDK (terminal backend `vercel_sandbox`) | P21-08 |
| vercel-internal-core | 0.2.0 | MIT | extra `vercel`, via vercel-sandbox | P21-08 |
| vercel-oidc | 0.9.0 | MIT | extra `vercel`, via vercel-sandbox (OIDC token lookup) | P21-08 |
| vercel-headers | 0.7.2 | MIT | extra `vercel`, via vercel-sandbox | P21-08 |
| datasets | 5.0.1 | Apache-2.0 | extra `research`: Hugging Face dataset loading for `indus research sample` | P22-15 |
| transformers | 5.17.0 | Apache-2.0 | extra `research`: the pinned tokenizer for `indus research compress` (remote code off by default) | P22-11 |
| safetensors | 0.8.0 | Apache-2.0 | extra `research`, via transformers | P22-11 |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python (CNRI-Python is not on the allow-list; owner check open, see LEAD-NOTES) | extra `research`, via transformers | P22-11 |
| typer | 0.27.2 | MIT | extra `research`, via transformers / huggingface-hub (their command lines; unused) | P22-11 |
| shellingham | 1.5.4 | ISC | extra `research`, via typer | P22-11 |
| pyarrow | 25.0.1 | Apache-2.0 (the wheels bundle Arrow's C++ dependencies, among them zlib (Zlib) and Boost (BSL-1.0), neither on the allow-list; owner check open, see LEAD-NOTES) | extra `research`, via datasets (columnar storage) | P22-15 |
| pandas | 3.0.6 | BSD-3-Clause (bundled notices: MIT, BSD, PSF-2.0 / CNRI) | extra `research`, via datasets | P22-15 |
| tzdata | 2026.5 | Apache-2.0 | extra `research`, via pandas | P22-15 |
| dill | 0.4.1 | BSD-3-Clause | extra `research`, via datasets / multiprocess | P22-15 |
| multiprocess | 0.70.19 | BSD-3-Clause | extra `research`, via datasets | P22-15 |
| xxhash | 4.0.1 | BSD-2-Clause | extra `research`, via datasets | P22-15 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | extras `voice`, `daytona`, `research`, via huggingface-hub / onnxruntime / opentelemetry-instrumentation / transformers / datasets (row added at the P22 R2 landing) | P22-11 |
| colorama | 0.4.6 | BSD-3-Clause (PyPI metadata) | Windows only: extras `voice`, `research`, via tqdm / typer (row added at the P22 R2 landing) | P22-11 |
