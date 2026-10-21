"""Process-local Lamport clocks for logical events and message timestamps.

Every process incarnation starts a new clock and PROCESS_ID. The ID distinguishes
restarted processes whose logical counters may overlap; these counters do not
measure elapsed time or establish causality between unrelated events.
"""

import socket
import threading
import uuid


def _validate_timestamp(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("A logical timestamp must be an integer, excluding bool.")
    if value < 0:
        raise ValueError("A logical timestamp must be nonnegative.")


class LamportClock:
    """One logical counter shared safely by all threads in a process."""

    def __init__(self, initial: int = 0):
        _validate_timestamp(initial)
        self._value = initial
        self._lock = threading.Lock()

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    def tick(self) -> int:
        """Record a local or send event and return its timestamp."""
        with self._lock:
            self._value += 1
            return self._value

    def receive(self, remote: int) -> int:
        """Record receipt of a timestamped message: max(local, remote) + 1."""
        _validate_timestamp(remote)
        with self._lock:
            self._value = max(self._value, remote) + 1
            return self._value


CLOCK = LamportClock()
PROCESS_ID = f"{socket.gethostname()}:{uuid.uuid4().hex}"
