"""Background tasks for the window: long operations (model loading, generation, a mission run) never block the interface, can be cancelled, and
are accounted for at shutdown. Results are handed back through `dispatch` so the interface toolkit can run them on its own thread."""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable


class CancelToken:
    def __init__(self):
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()


@dataclass
class Task:
    name: str
    thread: threading.Thread
    token: CancelToken
    started: float = field(default_factory=time.monotonic)
    done: bool = False
    error: str = ""


class BackgroundTasks:
    """`dispatch(fn)` must run fn on the thread that owns the interface (a Qt signal in the window; a simple queue in tests)."""

    def __init__(self, dispatch: Callable[[Callable[[], None]], None] | None = None):
        self._dispatch = dispatch
        self._queue: list[Callable[[], None]] = []
        self._qlock = threading.Lock()
        self._tasks: list[Task] = []
        self._lock = threading.Lock()
        self.closed = False

    # -- delivery of results to the interface thread
    def _post(self, fn: Callable[[], None]) -> None:
        if self._dispatch is not None:
            try:
                self._dispatch(fn)
            except Exception:  # noqa: BLE001 - the interface may already be gone
                pass
            return
        with self._qlock:
            self._queue.append(fn)

    def pump(self) -> int:
        """Run queued callbacks on the calling thread (used when no toolkit dispatcher was given, e.g. in tests)."""
        with self._qlock:
            items, self._queue = self._queue, []
        for fn in items:
            fn()
        return len(items)

    # -- running
    def submit(self, name: str, fn: Callable[[CancelToken], object], on_done: Callable[[object, str], None] | None = None) -> Task | None:
        """Runs fn(token) on a worker thread. on_done(result, error) is delivered to the interface thread. Returns None if shutting down."""
        if self.closed:
            return None
        token = CancelToken()

        def run():
            result, err = None, ""
            try:
                result = fn(token)
            except BaseException as e:  # noqa: BLE001 - a worker must never take the application down
                err = f"{type(e).__name__}: {e}"
                task.error = err + "\n" + traceback.format_exc(limit=3)
            task.done = True
            if on_done is not None:
                self._post(lambda: on_done(result, err))

        t = threading.Thread(target=run, name=f"jarvis-{name}", daemon=True)
        task = Task(name, t, token)
        with self._lock:
            self._tasks = [x for x in self._tasks if not x.done] + [task]
        t.start()
        return task

    def running(self) -> list[str]:
        with self._lock:
            return [x.name for x in self._tasks if not x.done and x.thread.is_alive()]

    def cancel_all(self) -> None:
        with self._lock:
            for x in self._tasks:
                x.token.cancel()

    def shutdown(self, timeout: float = 5.0) -> list[str]:
        """Stop accepting work, ask everything to stop, wait up to `timeout` seconds in total. Returns the names of tasks that did NOT finish
        (an operation that cannot be interrupted keeps running until it ends; the caller must say so)."""
        self.closed = True
        self.cancel_all()
        end = time.monotonic() + max(0.0, timeout)
        with self._lock:
            tasks = list(self._tasks)
        for x in tasks:
            x.thread.join(max(0.0, end - time.monotonic()))
        return [x.name for x in tasks if x.thread.is_alive()]
