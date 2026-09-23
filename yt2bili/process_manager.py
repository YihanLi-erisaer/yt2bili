"""Cancel-aware subprocess helpers. Never use a shell for media commands."""
from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager

from yt2bili import events

_owner_job = None


@contextmanager
def own_children():
    """On Windows, keep a kill-on-close job alive for the CLI process lifetime.

    Assign the parent before spawning any tool so every descendant inherits it.
    The handle deliberately stays open on normal context exit (closing it would
    terminate this process too); the OS closes it when this process exits.
    """
    global _owner_job
    if os.name == "nt" and _owner_job is None:
        import ctypes
        from ctypes import wintypes as w
        from yt2bili.exceptions import Yt2BiliError

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            if handle:
                kernel.CloseHandle(handle)
            raise Yt2BiliError("无法创建工具进程所有权，请检查系统进程组权限。")
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            kernel.CloseHandle(handle)
            raise Yt2BiliError("无法绑定工具进程组，已阻止执行。")
        _owner_job = handle
    yield


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied is not proof of death
        code = w.DWORD()
        try:
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def creation_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


@contextmanager
def watch(process):
    cancel = events.cancellation_event()
    stopped = threading.Event()

    def monitor():
        while not stopped.wait(0.1):
            if cancel and cancel.is_set() and process.poll() is None:
                process.kill()
                return

    thread = None
    if cancel:
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
    try:
        yield process
    finally:
        stopped.set()
        if thread:
            thread.join(timeout=1)
        if process.poll() is None:
            process.kill()
        process.wait()


def run(cmd, *, capture_output=False, text=False, encoding=None, errors=None, **kwargs):
    events.check_cancelled()
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with subprocess.Popen(cmd, text=text, encoding=encoding, errors=errors,
                          **creation_options(), **kwargs) as process:
        with watch(process):
            out, err = process.communicate()
        events.check_cancelled()
        return subprocess.CompletedProcess(cmd, process.returncode, out, err)
