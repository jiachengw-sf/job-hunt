"""The one place that checks whether a process is alive.

Never use ``os.kill(pid, 0)`` for this: on POSIX it only probes, but on Windows ``os.kill`` with
any signal other than CTRL_C/CTRL_BREAK *terminates* the process.
"""

from __future__ import annotations

import os

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        return _windows_alive(int(pid))
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _windows_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
