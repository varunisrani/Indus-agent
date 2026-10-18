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
dependencies at build time and listed here. Indus Agent has no runtime dependencies yet.
