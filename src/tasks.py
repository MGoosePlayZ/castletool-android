"""Cooperative cancel + progress for background work.

Python threads can't be killed, so long jobs call `check()` at safe points
(once per frame / segment / chunk). When the user cancels, the next `check()`
raises `Cancelled` and the job unwinds. `Cancelled` derives from BaseException
on purpose: the converters have plenty of `except Exception` blocks that must
not swallow it.

Progress: jobs call `step(i, n, "Processing frame 3/30")` (i = items done so
far). `span(a, b)` maps the inner 0..1 range onto a slice of the whole job, so
a video import can give frames 0-50% and sound 50-100%.

Everything here is a no-op when no Task is active, so castletool_core keeps
working as a plain CLI too.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional


class Cancelled(BaseException):
    """The user cancelled the running job."""


class Task:
    def __init__(self, label: str = "", on_update: Optional[Callable[["Task"], None]] = None,
                 on_message: Optional[Callable[[str], None]] = None, min_interval: float = 0.08):
        self.label = label
        self.cancelled = False
        self.frac: Optional[float] = None      # None = unknown (indeterminate)
        self.message = ""
        self._on_update = on_update
        self._on_message = on_message
        self._min_interval = min_interval
        self._last = 0.0

    def cancel(self) -> None:
        self.cancelled = True

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def update(self, frac: Optional[float] = None, message: Optional[str] = None, force: bool = False) -> None:
        if frac is not None:
            self.frac = max(0.0, min(1.0, frac))
        changed = message is not None and message != self.message
        if message is not None:
            self.message = message
        if changed and self._on_message:
            try:
                self._on_message(message)
            except Exception:
                pass
        now = time.monotonic()
        if self._on_update and (force or now - self._last >= self._min_interval):
            self._last = now
            try:
                self._on_update(self)
            except Exception:
                pass


_local = threading.local()


def current() -> Optional[Task]:
    return getattr(_local, "task", None)


@contextmanager
def use(task: Optional[Task]):
    """Make `task` the active one for this thread."""
    prev_task, prev_spans = getattr(_local, "task", None), getattr(_local, "spans", None)
    _local.task, _local.spans = task, []
    try:
        yield task
    finally:
        _local.task, _local.spans = prev_task, prev_spans


def check() -> None:
    t = current()
    if t is not None:
        t.check()


def _map(frac: float) -> float:
    for a, b in reversed(getattr(_local, "spans", None) or []):
        frac = a + (b - a) * max(0.0, min(1.0, frac))
    return frac


@contextmanager
def span(start: float, end: float):
    """Inside the block, progress 0..1 means start..end of the enclosing range."""
    spans = getattr(_local, "spans", None)
    if spans is None:
        yield
        return
    spans.append((start, end))
    try:
        yield
    finally:
        spans.pop()


def step(done: int, total: int, message: Optional[str] = None) -> None:
    """Checkpoint: cancel if asked, then report `done` of `total` items finished."""
    t = current()
    if t is None:
        return
    t.check()
    t.update(_map(done / total) if total else None, message)


def say(message: str, frac: Optional[float] = None) -> None:
    """Checkpoint with just a status line (progress bar keeps its position)."""
    t = current()
    if t is None:
        return
    t.check()
    t.update(_map(frac) if frac is not None else None, message)
