"""Facts about the machine this Python process runs on (the control host).

Rules: hardware facts never come from environment variables (``PROCESSOR_ARCHITECTURE`` and
friends lie under emulation and can be spoofed from a shell); nothing here spawns a process; every
file-reading fact takes ``root`` so tests can point it at a fake tree; results are cached per
process and ``clear_caches()`` forgets them. The only env input is the container and Termux
markers named below.

This describes where Indus runs, not the terminal target (SSH, container) and not a remote
desktop client.
"""

from __future__ import annotations

import functools
import importlib
import logging
import os
import platform
import sys
import sysconfig
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path("/")
GPU_CLASSES = ("nvidia", "amd", "intel", "apple_silicon", "none", "unknown")

_ARCH_ALIASES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "x64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
}
# IMAGE_FILE_MACHINE_* values returned by IsWow64Process2.
_WIN_MACHINE = {0x8664: "amd64", 0xAA64: "arm64", 0x014C: "x86"}
# sysconfig platform tags of the interpreter build on Windows.
_WIN_BUILD = {"win-amd64": "amd64", "win-arm64": "arm64", "win32": "x86"}
# PCI vendor ids of display controllers; discrete vendors win on hybrid machines.
_PCI_VENDOR = {"10de": "nvidia", "1002": "amd", "8086": "intel"}
_GPU_ORDER = ("nvidia", "amd", "intel")
_MAX_GPU_CARDS = 16
# Arm "CPU implementer" codes as Linux prints them in /proc/cpuinfo.
_ARM_VENDORS = {
    0x41: "ARM",
    0x42: "Broadcom",
    0x43: "Cavium",
    0x46: "Fujitsu",
    0x48: "HiSilicon",
    0x4E: "NVIDIA",
    0x51: "Qualcomm",
    0x61: "Apple",
    0x69: "Intel",
    0x6D: "Microsoft",
    0xC0: "Ampere",
}
_CGROUP_MARKERS = ("docker", "podman", "/lxc/", "kubepods", "containerd", "crio", "libpod")
_ROOT_MOUNT_MARKERS = ("docker", "kubepods", "containerd", "crio", "libpod")
_NO_AUDIT_SESSION = "4294967295"


def _ctypes_any() -> Any:
    """``ctypes`` untyped: its Windows-only members are unknown to a POSIX type check."""
    import ctypes

    return ctypes


# -- small readers ------------------------------------------------------------------------------


def _under(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


def _read(root: Path, absolute: str, limit: int = 65536) -> str | None:
    """Text of a file under ``root``; None when it cannot be read."""
    try:
        with _under(root, absolute).open("r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


# -- OS and architecture ------------------------------------------------------------------------


def normalize_arch(raw: str | None) -> str:
    return _ARCH_ALIASES.get((raw or "").strip().lower(), "unknown")


@functools.cache
def os_family() -> str:
    """``linux``, ``darwin``, ``win32`` (other platforms: ``sys.platform`` as is)."""
    return "linux" if sys.platform.startswith("linux") else sys.platform


@functools.cache
def process_arch() -> str:
    """The architecture this interpreter was built for (emulated or not)."""
    if os_family() == "win32":
        return _WIN_BUILD.get(sysconfig.get_platform(), "unknown")
    return normalize_arch(platform.machine())


def _windows_native_machine() -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32: Any = _ctypes_any().WinDLL("kernel32", use_last_error=True)
    is_wow64_process2 = getattr(kernel32, "IsWow64Process2", None)
    if is_wow64_process2 is None:
        return None
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    is_wow64_process2.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        ctypes.POINTER(wintypes.USHORT),
    ]
    is_wow64_process2.restype = wintypes.BOOL
    process_machine = wintypes.USHORT(0)
    native_machine = wintypes.USHORT(0)
    ok = is_wow64_process2(
        kernel32.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)
    )
    if not ok:
        return None
    return int(native_machine.value) or None


def _sysctl_raw(name: str, size: int) -> bytes | None:
    """``sysctlbyname`` through the process's own libc symbols (macOS); no library search."""
    import ctypes

    libc: Any = ctypes.CDLL(None)
    buffer = ctypes.create_string_buffer(size)
    length = ctypes.c_size_t(size)
    if libc.sysctlbyname(name.encode(), buffer, ctypes.byref(length), None, 0) != 0:
        return None
    return buffer.raw[: length.value]


def _sysctl_int(name: str, size: int = 4) -> int | None:
    raw = _sysctl_raw(name, size)
    return int.from_bytes(raw, sys.byteorder) if raw else None


def _sysctl_text(name: str) -> str:
    raw = _sysctl_raw(name, 256)
    return raw.split(b"\0", 1)[0].decode(errors="replace").strip() if raw else ""


@functools.cache
def native_arch() -> str:
    """The hardware architecture, correct under x64-on-ARM64 emulation and Rosetta."""
    family = os_family()
    if family == "win32":
        try:
            native = _windows_native_machine()
        except (AttributeError, OSError, TypeError, ValueError):
            native = None
        return _WIN_MACHINE.get(native or 0) or normalize_arch(platform.machine())
    if family == "darwin" and process_arch() == "amd64":
        try:
            translated = _sysctl_int("sysctl.proc_translated")
        except (AttributeError, OSError, TypeError, ValueError):
            translated = None
        if translated == 1:
            return "arm64"
    return process_arch()


# -- CPU and memory -----------------------------------------------------------------------------


def parse_cpu_model(cpuinfo: str, device_tree_model: str = "") -> str:
    for key in ("model name", "hardware"):
        for line in cpuinfo.splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip().lower() == key and value.strip():
                return value.strip()
    return device_tree_model.replace("\0", "").strip()


def parse_cpu_vendor(cpuinfo: str) -> str:
    fields: dict[str, str] = {}
    for line in cpuinfo.splitlines():
        name, sep, value = line.partition(":")
        if sep:
            fields.setdefault(name.strip(), value.strip())
    if fields.get("vendor_id"):
        return fields["vendor_id"]
    try:
        return _ARM_VENDORS.get(int(fields.get("CPU implementer", ""), 16), "")
    except ValueError:
        return ""


def parse_meminfo_total(meminfo: str) -> int | None:
    for line in meminfo.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "MemTotal:" and parts[1].isdigit():
            unit = parts[2].lower() if len(parts) > 2 else "kb"
            return int(parts[1]) * (1024 if unit == "kb" else 1)
    return None


def _windows_registry_text(subkey: str, value: str) -> str:
    winreg: Any = importlib.import_module("winreg")

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
            return str(winreg.QueryValueEx(key, value)[0]).strip()
    except OSError:
        return ""


_WIN_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"


@functools.cache
def cpu_model(root: Path = ROOT) -> str:
    """CPU model name, or "" when it cannot be read."""
    family = os_family()
    if family == "win32":
        return _windows_registry_text(_WIN_CPU_KEY, "ProcessorNameString")
    if family == "darwin":
        return _sysctl_text("machdep.cpu.brand_string")
    return parse_cpu_model(
        _read(root, "/proc/cpuinfo") or "", _read(root, "/proc/device-tree/model", 512) or ""
    )


@functools.cache
def cpu_vendor(root: Path = ROOT) -> str:
    """CPU vendor (x86 ``vendor_id`` or the Arm implementer), or ""."""
    family = os_family()
    if family == "win32":
        return _windows_registry_text(_WIN_CPU_KEY, "VendorIdentifier")
    if family == "darwin":
        return "Apple" if native_arch() == "arm64" else _sysctl_text("machdep.cpu.vendor")
    return parse_cpu_vendor(_read(root, "/proc/cpuinfo") or "")


def _windows_ram_total() -> int | None:
    import ctypes

    # MEMORYSTATUSEX: two 32-bit words (struct size, load) then seven 64-bit counters, the first
    # of which is total physical memory. Only that one is read.
    class _MemoryStatus(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
            (f"counter{index}", ctypes.c_uint64) for index in range(7)
        ]

    block = _MemoryStatus()
    block.size = ctypes.sizeof(block)
    kernel32: Any = _ctypes_any().windll.kernel32
    if kernel32.GlobalMemoryStatusEx(ctypes.byref(block)) == 0:
        return None
    total = int(block.counter0)
    return total if total > 0 else None


def _sysconf_ram() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    return pages * page_size if pages > 0 and page_size > 0 else None


@functools.cache
def ram_total_bytes(root: Path = ROOT) -> int | None:
    """Physical memory in bytes, or None when unmeasured."""
    family = os_family()
    try:
        if family == "win32":
            return _windows_ram_total()
        if family == "darwin":
            return _sysctl_int("hw.memsize", 8) or _sysconf_ram()
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    total = parse_meminfo_total(_read(root, "/proc/meminfo", 4096) or "")
    if total is None and root == ROOT:
        total = _sysconf_ram()
    return total


# -- GPU ----------------------------------------------------------------------------------------


def classify_gpu_vendors(vendor_ids: list[str]) -> str:
    """Highest-priority class among PCI vendor ids; ``unknown`` when none could be read."""
    cleaned = [v.strip().lower().removeprefix("0x") for v in vendor_ids]
    if cleaned and not any(cleaned):
        return "unknown"
    found = {_PCI_VENDOR.get(v) for v in cleaned}
    return next((name for name in _GPU_ORDER if name in found), "none")


def _linux_drm_vendors(root: Path) -> list[str] | None:
    """Vendor ids of DRM cards; None when the DRM tree or a vendor file cannot be read."""
    drm = _under(root, "/sys/class/drm")
    try:
        names = sorted(entry.name for entry in drm.iterdir())
    except OSError:
        return None
    cards = [n for n in names if n.startswith("card") and n[4:].isdigit()][:_MAX_GPU_CARDS]
    vendors: list[str] = []
    for card in cards:
        text = _read(root, f"/sys/class/drm/{card}/device/vendor", 16)
        if text is None:
            return None
        vendors.append(text)
    return vendors


_WIN_DISPLAY_CLASS = (
    r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
)


def _windows_display_vendors() -> list[str] | None:
    winreg: Any = importlib.import_module("winreg")

    vendors: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WIN_DISPLAY_CLASS) as key:
            for index in range(_MAX_GPU_CARDS):
                try:
                    sub = winreg.EnumKey(key, index)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                ident = _windows_registry_text(
                    f"{_WIN_DISPLAY_CLASS}\\{sub}", "MatchingDeviceId"
                ).upper()
                marker = ident.find("VEN_")
                if marker >= 0:
                    vendors.append(ident[marker + 4 : marker + 8])
                elif not ident:
                    vendors.append("")
    except OSError:
        return None
    return vendors


@functools.cache
def gpu_class(root: Path = ROOT) -> str:
    """One of ``GPU_CLASSES``. Any read failure gives ``unknown``, which must never block."""
    try:
        family = os_family()
        if family == "darwin":
            # Intel Macs would need an IOKit scan; not measured yet.
            return "apple_silicon" if native_arch() == "arm64" else "unknown"
        if family == "win32":
            vendors = _windows_display_vendors()
        elif (
            _under(root, "/proc/driver/nvidia/version").exists()
            or _under(root, "/usr/lib/wsl/lib/libcuda.so.1").exists()
        ):
            return "nvidia"
        else:
            vendors = _linux_drm_vendors(root)
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return "unknown"
    return "unknown" if vendors is None else classify_gpu_vendors(vendors)


# -- runtime predicates -------------------------------------------------------------------------


@functools.cache
def is_wsl(root: Path = ROOT) -> bool:
    return "microsoft" in (_read(root, "/proc/version", 4096) or "").lower()


def root_mount_is_container(mountinfo: str) -> bool:
    """cgroup v2 rule: only the line whose mount point is ``/`` counts.

    Hosts that merely run containers list docker/containerd mounts elsewhere; those lines must
    not classify the host as a container.
    """
    for line in mountinfo.splitlines():
        before, sep, after = line.partition(" - ")
        fields = before.split()
        if not sep or len(fields) < 5 or fields[4] != "/":
            continue
        fs_fields = after.split()
        fs_type = fs_fields[0] if fs_fields else ""
        source = fs_fields[1] if len(fs_fields) > 1 else ""
        if fs_type == "overlay":
            return True
        haystack = f"{fields[3]} {source}"
        if any(marker in haystack for marker in _ROOT_MOUNT_MARKERS):
            return True
    return False


@functools.cache
def _container_files(root: Path) -> bool:
    if _under(root, "/.dockerenv").exists() or _under(root, "/run/.containerenv").exists():
        return True
    cgroup = _read(root, "/proc/1/cgroup") or ""
    if any(marker in cgroup for marker in _CGROUP_MARKERS):
        return True
    return root_mount_is_container(_read(root, "/proc/self/mountinfo", 1 << 20) or "")


def is_container(root: Path = ROOT, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    if values.get("KUBERNETES_SERVICE_HOST", "").strip():
        return True
    return _container_files(root)


def is_termux(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return bool(values.get("TERMUX_VERSION")) or "com.termux/files/usr" in values.get("PREFIX", "")


def _windows_interactive() -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32: Any = _ctypes_any().WinDLL("kernel32", use_last_error=True)
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
        return False
    if session.value == 0:
        return False
    active = kernel32.WTSGetActiveConsoleSessionId()
    if active != 0xFFFFFFFF and active == session.value:
        return True
    user32: Any = _ctypes_any().WinDLL("user32", use_last_error=True)
    return bool(user32.GetProcessWindowStation())


def interactive_session(root: Path = ROOT, uid: int | None = None) -> bool:
    """Whether this process can reach a logged-in user's session (desktop prompts, keychain)."""
    family = os_family()
    if family == "win32":
        try:
            return _windows_interactive()
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    if family == "linux":
        session = (_read(root, "/proc/self/sessionid", 64) or "").strip()
        if not session or session == _NO_AUDIT_SESSION:
            return False
        user = uid if uid is not None else getattr(os, "getuid", lambda: -1)()
        return _under(root, f"/run/user/{user}").is_dir()
    return True


# -- summary ------------------------------------------------------------------------------------


def machine_kind() -> str:
    return {"darwin": "Mac", "win32": "PC"}.get(os_family(), "computer")


def summary(root: Path = ROOT) -> dict[str, object]:
    """Every fact that could be measured; a fact that raises or is empty is left out."""
    module = sys.modules[__name__]
    readers: dict[str, Callable[[], object]] = {
        "os_family": lambda: module.os_family(),
        "process_arch": lambda: module.process_arch(),
        "native_arch": lambda: module.native_arch(),
        "cpu_model": lambda: module.cpu_model(root),
        "cpu_vendor": lambda: module.cpu_vendor(root),
        "ram_total_bytes": lambda: module.ram_total_bytes(root),
        "gpu_class": lambda: module.gpu_class(root),
        "wsl": lambda: module.is_wsl(root),
        "container": lambda: module.is_container(root),
        "termux": lambda: module.is_termux(),
        "machine_kind": lambda: module.machine_kind(),
    }
    out: dict[str, object] = {}
    for key, read in readers.items():
        try:
            value = read()
        except Exception:
            log.debug("host fact %s could not be measured", key, exc_info=True)
            continue
        if value is None or value == "" or value == "unknown":
            continue
        out[key] = value
    return out


def clear_caches() -> None:
    module = sys.modules[__name__]
    for name in (
        "os_family",
        "process_arch",
        "native_arch",
        "cpu_model",
        "cpu_vendor",
        "ram_total_bytes",
        "gpu_class",
        "is_wsl",
        "_container_files",
    ):
        fact = getattr(module, name)
        clear = getattr(fact, "cache_clear", None)  # a test may have replaced the function
        if clear is not None:
            clear()
