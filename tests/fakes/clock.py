from threading import Condition, Event


class FakeClock:
    """A monotonic clock and a Unix wall clock that advance together."""

    def __init__(self, *, start=0.0, wall_start=1_700_000_000.0):
        self._now = float(start)
        self._wall_offset = wall_start - start
        self._changed = Condition()
        self._delays: list[float] = []

    def now(self) -> float:
        with self._changed:
            return self._now

    def wall_time(self) -> float:
        return self._wall_offset + self.now()

    def sleep(self, seconds: float) -> None:
        with self._changed:
            self._now += max(0.0, seconds)
            self._changed.notify_all()

    def wait(self, event: Event, timeout_s: float) -> bool:
        if timeout_s <= 0:
            return event.is_set()
        deadline = self.now() + timeout_s
        # shortcut: let daemon workers publish replies or delays before advancing; use a scheduler for race-order tests.
        while not event.wait(0.02):
            with self._changed:
                if self._now >= deadline:
                    return event.is_set()
                self._now = min((at for at in self._delays if self._now < at < deadline), default=deadline)
                self._changed.notify_all()
        return True

    def _delay(self, seconds: float) -> None:
        with self._changed:
            due = self._now + seconds
            self._delays.append(due)
            try:
                self._changed.wait_for(lambda: self._now >= due)
            finally:
                self._delays.remove(due)
