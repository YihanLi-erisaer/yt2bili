"""Cancel-aware subprocess helpers. Never use a shell for media commands."""
from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager

from yt2bili import events


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

