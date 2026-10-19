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
| huggingface-hub | 1.33.0 | Apache-2.0 | extra `voice`, via faster-whisper / tokenizers (model download) | P10-18 |
| hf-xet | 1.6.0 | Apache-2.0 | extra `voice`, via huggingface-hub | P10-18 |
| filelock | 4.0.10 | MIT | extra `voice`, via huggingface-hub | P10-18 |
| fsspec | 2026.9.0 | BSD-3-Clause | extra `voice`, via huggingface-hub | P10-18 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT (unmodified) | extra `voice`, via faster-whisper / huggingface-hub | P10-18 |
| pyyaml | 6.0.3 | MIT | extra `voice`, via ctranslate2 / huggingface-hub | P10-18 |
| onnxruntime | 1.30.0 | MIT | extra `voice`, via faster-whisper (voice activity filter) | P10-18 |
| flatbuffers | 25.12.19 | Apache-2.0 | extra `voice`, via onnxruntime | P10-18 |
| protobuf | 7.36.2 | BSD-3-Clause | extra `voice`, via onnxruntime | P10-18 |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (Zlib is not on the allow-list; owner check open, see LEAD-NOTES) | extra `voice`, via ctranslate2 / onnxruntime | P10-18 |
| tokenizers | 0.23.2 | Apache-2.0 | extra `voice`, via faster-whisper | P10-18 |
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
