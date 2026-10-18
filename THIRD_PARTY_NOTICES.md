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
| typing-extensions | 4.16.0 | PSF-2.0 | via pydantic / pydantic-core / typing-inspection | P01-09 |
| typing-inspection | 0.4.4 | MIT | via pydantic | P01-09 |
| ruamel.yaml | 0.19.1 | MIT | YAML 1.1 load and round-trip dump (`yamlio`) | P01-08 |
