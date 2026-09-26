"""Hardware detection for local PyTorch venv bootstraps."""

from __future__ import annotations

import platform
import shutil
import subprocess


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def has_nvidia_gpu() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        proc = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def detect_torch_device() -> str:
    """Return cuda, mps, or cpu for PyTorch wheel selection."""
    if has_nvidia_gpu():
        return "cuda"
    if is_apple_silicon():
        return "mps"
    return "cpu"


def system_memory_gb() -> float:
    """Best-effort unified memory size (GB) for model tier selection."""
    if platform.system() == "Darwin":
        try:
            proc = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip().isdigit():
                return int(proc.stdout.strip()) / (1024**3)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if platform.system() == "Windows":
        win_gb = _windows_memory_gb()
        if win_gb:
            return win_gb
        return 8.0
    try:
        import os

        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return (pages * size) / (1024**3)
    except (AttributeError, OSError, ValueError):
        return 8.0


def _windows_memory_gb() -> float:
    """Physical RAM (GB) via GlobalMemoryStatusEx; 0.0 when unavailable."""
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return 0.0
        return float(stat.ullTotalPhys) / (1024**3)
    except (AttributeError, OSError, ValueError):
        return 0.0


def torch_index_url(device: str | None = None) -> str:
    dev = device or detect_torch_device()
    if dev == "cuda":
        return "https://download.pytorch.org/whl/cu124"
    if dev == "mps":
        return "https://download.pytorch.org/whl/cpu"
    return "https://download.pytorch.org/whl/cpu"
