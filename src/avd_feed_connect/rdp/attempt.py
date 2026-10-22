"""Cancellation for one pending FreeRDP launch, never other sessions."""
import os
import signal
import subprocess
import threading
import time


class ConnectionAttempt:
    def __init__(self):
        self.cancelled = threading.Event()
        self.is_current = lambda: True
        self.close_auth = None
        self._lock = threading.Lock()
        self._process = None

    def active(self):
        return not self.cancelled.is_set() and self.is_current()

    def attach(self, process):
        with self._lock:
            self._process = process
            cancelled = self.cancelled.is_set()
        if cancelled:
            self._stop(process)

    def publish(self, callback):
        """Publish process handles atomically with respect to cancellation."""
        with self._lock:
            if not self.active():
                return False
            callback()
            return True

    def cancel(self):
        with self._lock:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            process = self._process
        if process is not None:
            self._stop(process)

    @staticmethod
    def _stop(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        def reap():
            deadline = time.monotonic() + 2
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            # The leader may exit while a descendant still holds the PTY.
            # Escalate the owned group independently of the leader's exit.
            threading.Event().wait(max(0, deadline - time.monotonic()))
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        threading.Thread(target=reap, daemon=True).start()
