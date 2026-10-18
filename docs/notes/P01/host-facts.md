# Task: Host facts (P01-01) Phase: 01 Owner: P01 S-core builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_platform/host/facts.py` (all),
`hermes_platform/host/runtime.py` (all), `hermes_platform/host/summary.py` (first 120 lines: account
and locale helpers, summary shape), `hermes_platform/host/gpu_adapters.py` (header only),
`hermes_platform/AGENTS.md`. Closed before writing.
Evidence: R09.17.

What it does (own words): one place that answers "what machine is this Python running on": OS
family, the architecture of this process and of the hardware underneath it (which differ under
x64-on-ARM64 emulation and Rosetta), CPU model and vendor, total RAM, a coarse GPU class, and
runtime predicates (WSL, container, Termux, interactive login session). Hardware answers never come
from environment variables, because `PROCESSOR_ARCHITECTURE` and similar lie under emulation and a
shell could spoof them. No subprocesses either. Every file-reading fact takes a `root` path so tests
point it at a fake tree.

Inputs → outputs: `root: Path` (default `/`), optional `env` mapping for the container/Termux
markers only → strings, ints, bools; `summary(root)` → dict without keys that could not be measured.

Files: `src/indus/platform/facts.py`, `tests/unit/platform/test_facts.py`,
`tests/fixtures/platform/roots/{linux-nvidia,linux-amd-intel,cgroup2-host,cgroup2-container,wsl}/`.

Edge cases / failure modes:
- unreadable sysfs vendor file or missing DRM tree → `gpu_class` is `unknown`, never an exception.
- hosts that run containers have docker/containerd lines in mountinfo for other mounts; only the
  line whose mount point is `/` counts.
- Windows ctypes calls missing (old Windows) → fall back to the machine string.
- macOS: `sysctl.proc_translated` absent on Intel → not translated.
- a fact that raises inside `summary()` is dropped, the rest still reported.

Our design differences / decisions:
- Readers take `root` instead of hard-coded absolute paths; results cached per (function, root).
- Intel-Mac IOKit GPU enumeration and the NVIDIA platform classifier are not built (P1 needs only
  the class); Intel Macs report `unknown` (never blocks).
- macOS sysctl through `ctypes.CDLL(None)`, so no library search (which can spawn helpers on Linux).
- mountinfo root-line rule: container when the root mount's filesystem is `overlay` or its source /
  root field names a container runtime.
- `machine_kind`: `Mac` (darwin), `PC` (win32), `computer` otherwise.

Tests we will write: fake roots for NVIDIA procfs, sysfs 8086+1002 → amd, unreadable vendor →
unknown, no DRM → unknown, empty DRM → none; cgroup2 host vs container; `.dockerenv`; WSL;
`PROCESSOR_ARCHITECTURE` ignored; arch normalisation table; cpuinfo/meminfo parsing; summary drops
failing keys; `clear_caches()`.

## Decisions for the lead
- Intel-Mac GPU detection returns `unknown` until a later phase needs more (IOKit via ctypes).

Compare checklist (filled after the rewrite; reopened `hermes_platform/host/facts.py`, `runtime.py`):
- [x] arch never from env; IsWow64Process2 machine codes; Rosetta → arm64 (test:
  `tests/unit/platform/test_facts.py::test_processor_architecture_env_is_ignored`)
- [x] GPU priority nvidia > amd > intel, procfs/WSL library short-circuit, read error → unknown
  (tests: `test_gpu_nvidia_procfs`, `test_gpu_sysfs_priority_amd_over_intel`, `test_gpu_unreadable_sysfs_is_unknown`)
- [x] cgroup v2 root-mount rule (tests: `test_cgroup2_host_is_not_container`, `test_cgroup2_container_root_overlay`)
- [x] interactive session rule (audit id + `/run/user/<uid>`) (test: `test_interactive_session_linux`)
- Deliberate differences: theirs scans only the root line for runtime names; ours also treats an
  `overlay` root filesystem as a container (the phase row asks for it). Theirs reads fixed paths;
  ours takes `root`.
- Not taken: NVIDIA GPU list / Spark classifier, account full name and locale, Intel-Mac IOKit scan.

## Status

Done (2026-10-18). Facts, predicates and summary with fake-root tests.
