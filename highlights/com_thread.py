"""Dedicated STA worker thread for SolidWorks COM calls.

SolidWorks's SldWorks.Application is an STA (Single-Threaded Apartment) COM
server. All calls must originate from the same thread that first initialized
COM for that apartment. When code paths cross threads — as they do when an
HTTP server dispatches work via asyncio.to_thread or a threadpool — pywin32
returns degraded <unknown> COM proxies whose named members fail.

This module provides a worker thread that:
  1. Initializes COM as STA once at startup (pythoncom.CoInitialize).
  2. Serves work items from a queue: each item is a callable; the worker
     invokes it on its own thread and returns the result via a Future.
  3. Cleanly tears down COM on stop.

Adapters instantiate one ComWorker per session. Public adapter methods
wrap their COM-touching logic in a submit() call so every COM operation
runs on the worker thread, regardless of which caller thread invoked the
adapter method.

Reliability (call timeout + poison + restart)
---------------------------------------------
A SolidWorks STA call can hang indefinitely (a suppressed-but-stuck modal
dialog, a COM deadlock). A naive ``future.result()`` would then block the
calling HTTP request forever. ``submit`` therefore supports a *call timeout*:

* The ceiling is **opt-in** via ``INTENTCAD_SW_COM_CALL_TIMEOUT_S`` and is
  **off by default**, because the CFD/Flow-Simulation path runs its (possibly
  multi-minute) solve through the shared worker (``run_on_com_thread``); a
  blanket default could abort a valid solve. Operators who only run the
  bounded modeling/query path can set a ceiling (e.g. ``300``).
* A Python thread cannot be force-killed, so when a call times out the worker
  is marked **poisoned**: its thread stays blocked on the stuck call (as a
  daemon, so it never blocks process exit) and the owner replaces the worker
  with a fresh one on next use (:func:`get_shared_com_worker`, and the
  adapter's ``_ensure_worker``). :meth:`is_healthy` lets owners detect a
  poisoned or dead worker and restart it instead of returning a wedged one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Final, TypeVar, cast

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SHUTDOWN_SENTINEL = object()

# Sentinel default for ``submit(timeout=...)`` meaning "use the worker's
# configured default". A negative value is never a meaningful real timeout, so
# this stays fully typed as ``float`` without an ``object`` sentinel.
_USE_WORKER_DEFAULT: Final[float] = -1.0

ENV_CALL_TIMEOUT: Final[str] = "INTENTCAD_SW_COM_CALL_TIMEOUT_S"


class ComCallTimeoutError(TimeoutError):
    """A COM call did not return within the worker's call-timeout budget.

    Because a SolidWorks STA call cannot be force-killed from Python, the worker
    that hosted the timed-out call is left *poisoned*; the owner creates a fresh
    worker on next use. Surfaced to the tool layer as ``sw_com_error`` so the
    agent treats it as a transient COM failure (retry / re-read state).
    """


def default_call_timeout_from_env() -> float | None:
    """Optional global COM call-timeout ceiling, read from the environment.

    Returns ``None`` (no ceiling — the original behavior) when
    :data:`ENV_CALL_TIMEOUT` is unset, ``0``, negative, or unparseable. A
    positive value caps every ``submit`` that does not pass an explicit
    per-call timeout.
    """
    raw = os.environ.get(ENV_CALL_TIMEOUT)
    if raw is None:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if val > 0 else None


class ComWorker:
    """A dedicated STA thread that serves COM-bound callables from a queue.

    Usage:
        worker = ComWorker()
        worker.start()
        try:
            result = worker.submit(lambda: sw_app.NewDocument(template, 0, 0.0, 0.0))
        finally:
            worker.stop()

    submit() blocks the caller until the callable completes on the worker
    thread and returns the result, or raises whatever the callable raised. If a
    call exceeds the (optional) timeout it raises :class:`ComCallTimeoutError` and the
    worker becomes permanently poisoned (see module docstring).
    """

    def __init__(
        self,
        *,
        name: str = "solidworks-com-worker",
        default_call_timeout_s: float | None = None,
    ) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._started = threading.Event()
        self._init_error: BaseException | None = None
        self._stopped = False
        self._poisoned = False
        self._name = name
        self._default_call_timeout_s = default_call_timeout_s

    @property
    def thread_ident(self) -> int | None:
        """OS thread id of the COM worker thread, or None before start completes."""
        return self._thread_ident

    @property
    def poisoned(self) -> bool:
        """True once a COM call has timed out; the worker can no longer be used."""
        return self._poisoned

    def is_healthy(self) -> bool:
        """True when the worker can still accept and serve work.

        False if it never started, hit a COM init error, was stopped, was
        poisoned by a timed-out call, or its thread has died. Owners call this
        to decide whether to reuse the worker or replace it with a fresh one.
        """
        return (
            not self._poisoned
            and not self._stopped
            and self._init_error is None
            and self._thread is not None
            and self._thread.is_alive()
        )

    def start(self) -> None:
        """Start the worker thread and wait until COM is initialized."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=self._name,
            daemon=True,
        )
        self._thread.start()
        if not self._started.wait(timeout=10.0):
            raise RuntimeError("ComWorker failed to initialize within 10 seconds.")
        if self._init_error is not None:
            raise self._init_error

    def stop(self) -> None:
        """Stop the worker thread and clean up COM. Idempotent.

        A poisoned worker's thread is stuck on a hung COM call and will never
        drain the queue, so we abandon it (it is a daemon) instead of blocking
        the caller on a join that can only time out.
        """
        if self._stopped or self._thread is None:
            return
        self._stopped = True
        if self._poisoned:
            self._thread = None
            self._thread_ident = None
            return
        self._queue.put(_SHUTDOWN_SENTINEL)
        self._thread.join(timeout=5.0)
        self._thread = None
        self._thread_ident = None

    def submit(self, fn: Callable[[], T], *, timeout: float | None = _USE_WORKER_DEFAULT) -> T:
        """Run fn() on the worker thread and return its result.

        If called from the worker thread itself (reentrant case), runs fn()
        directly without re-queueing — prevents deadlock when adapter methods
        call into each other.

        ``timeout`` defaults to the worker's configured ceiling; pass a float to
        override, or ``None`` to wait indefinitely for this one call (e.g. a
        long-running solve). On expiry the worker is poisoned and
        :class:`ComCallTimeoutError` is raised.

        Re-raises any exception raised by fn().
        """
        if self._poisoned:
            raise RuntimeError(
                "ComWorker is poisoned (a prior COM call timed out); create a new worker."
            )
        if self._stopped or self._thread is None:
            raise RuntimeError("ComWorker is not running; call start() first.")
        # Reentrant case: already on the worker thread. Execute directly.
        if threading.get_ident() == self._thread_ident:
            return fn()
        eff_timeout = (
            self._default_call_timeout_s if timeout == _USE_WORKER_DEFAULT else timeout
        )
        future: Future[T] = Future()
        self._queue.put((fn, future))
        try:
            return future.result(timeout=eff_timeout)
        except FutureTimeoutError as exc:
            self._poisoned = True
            logger.error(
                "ComWorker %r: a COM call exceeded %.1fs and was abandoned; worker "
                "poisoned, a fresh worker will be created on next use.",
                self._name,
                eff_timeout if eff_timeout is not None else -1.0,
            )
            raise ComCallTimeoutError(
                f"SolidWorks COM call did not return within {eff_timeout:.0f}s."
            ) from exc

    def _run(self) -> None:
        """Worker thread main loop."""
        self._thread_ident = threading.get_ident()
        try:
            import pythoncom  # type: ignore[import-untyped]

            pythoncom.CoInitialize()
        except Exception as exc:
            self._init_error = exc
            self._started.set()
            return
        self._started.set()
        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN_SENTINEL:
                    break
                fn, future = cast(tuple[Callable[[], Any], Future[Any]], item)
                try:
                    result = fn()
                    future.set_result(result)
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                logger.debug("ComWorker: CoUninitialize failed", exc_info=True)


_shared_worker: ComWorker | None = None
_shared_worker_lock = threading.Lock()


def get_shared_com_worker() -> ComWorker:
    """Process-wide STA worker for server routes that touch COM outside an adapter.

    Returns a healthy worker, replacing a dead or poisoned one transparently so
    a single hung COM call can never wedge every later caller.
    """
    global _shared_worker
    with _shared_worker_lock:
        existing = _shared_worker
        if existing is None or not existing.is_healthy():
            if existing is not None:
                with contextlib.suppress(Exception):
                    existing.stop()
            existing = ComWorker(default_call_timeout_s=default_call_timeout_from_env())
            existing.start()
            _shared_worker = existing
        return existing


def run_on_com_thread(fn: Callable[[], T]) -> T:
    """Run *fn* on the shared COM worker thread (required from asyncio thread pools)."""
    return get_shared_com_worker().submit(fn)


__all__ = [
    "ENV_CALL_TIMEOUT",
    "ComCallTimeoutError",
    "ComWorker",
    "default_call_timeout_from_env",
    "get_shared_com_worker",
    "run_on_com_thread",
]
