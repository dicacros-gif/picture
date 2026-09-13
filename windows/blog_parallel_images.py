"""Provider lanes and a coordinator-owned image queue, without application I/O."""
from __future__ import annotations

import copy
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor


class LaneCancelled(RuntimeError):
    pass


class _Lease:
    def __init__(self, lanes, provider):
        self.lanes, self.provider, self.released = lanes, provider, False

    def release(self):
        with self.lanes.condition:
            if not self.released:
                self.released = True
                self.lanes.busy.discard(self.provider)
                self.lanes.condition.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.release()


class _PriorityTicket:
    def __init__(self, lanes, provider, priority):
        self.lanes, self.provider, self.waiting, self.priority = lanes, provider, True, priority
        with lanes.condition:
            lanes.priority_waiters.setdefault(provider, []).append(self)

    def close(self):
        with self.lanes.condition:
            if self.waiting:
                self.waiting = False
                self.lanes.priority_waiters[self.provider].remove(self)
                self.lanes.condition.notify_all()

    def acquire(self, cancel_event):
        with self.lanes.condition:
            while (self.provider in self.lanes.busy or any(
                    ticket is not self and ticket.priority < self.priority
                    for ticket in self.lanes.priority_waiters.get(self.provider, []))):
                if cancel_event is not None and cancel_event.is_set():
                    self.close()
                    raise LaneCancelled("CLI 제공자 작업 대기가 취소되었습니다.")
                self.lanes.condition.wait(.05)
            if cancel_event is not None and cancel_event.is_set():
                self.close()
                raise LaneCancelled("CLI 제공자 작업 대기가 취소되었습니다.")
            self.close()
            self.lanes.busy.add(self.provider)
            return _Lease(self.lanes, self.provider)


class ProviderLanes:
    """One active request per provider; queued text/reviews precede new images."""
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.busy = set()
        self.priority_waiters = {}

    def priority(self, provider, *, images=False):
        # Register synchronously before starting a text worker or pumping images.
        return _PriorityTicket(self, provider, 1 if images else 0)

    def try_image(self, provider):
        with self.condition:
            if provider in self.busy or self.priority_waiters.get(provider, 0):
                return None
            self.busy.add(provider)
            return _Lease(self, provider)


_BRIDGE_LANES_LOCK = threading.Lock()


def provider_lanes(bridge):
    with _BRIDGE_LANES_LOCK:
        lanes = getattr(bridge, "_blog_provider_lanes", None)
        if not isinstance(lanes, ProviderLanes):
            lanes = bridge._blog_provider_lanes = ProviderLanes()
        return lanes


class CombinedCancelSignal:
    def __init__(self, *events):
        self.events = [event for event in events if event is not None]

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, timeout=None):
        import time
        end = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if end is not None and time.monotonic() >= end:
                return False
            time.sleep(min(.05, max(0, end - time.monotonic())) if end is not None else .05)
        return True


class ImageGenerationBatch:
    """Only pump/close (the coordinator) may reserve or store a candidate.

    Workers receive detached jobs, create files, and return detached candidates.
    No worker reads or mutates the shared article/manifest.
    """
    def __init__(self, jobs, *, lanes, reserve, generate, store, check):
        self.queues = {provider: deque() for provider in ("antigravity", "chatgpt")}
        for job in jobs:
            self.queues["antigravity" if job["index"] % 2 == 0 else "chatgpt"].append(copy.deepcopy(job))
        self.lanes, self.reserve, self.generate, self.store, self.check = lanes, reserve, generate, store, check
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="blog-image")
        self.running = {}
        self.closed = False
        self.error = None

    @property
    def pending(self):
        return bool(self.running) or any(self.queues.values())

    def _collect(self, *, raise_errors):
        for provider, (future, reserved) in list(self.running.items()):
            if not future.done():
                continue
            del self.running[provider]
            try:
                candidate = future.result()
            except Exception as exc:
                candidate = copy.deepcopy(reserved["candidate"])
                candidate.update(approved=False, error=str(exc))
                self.error = self.error or exc
            self.store(candidate)
        if raise_errors and self.error is not None:
            raise self.error

    def pump(self, *, dispatch=True, exclude=(), raise_errors=True):
        if self.closed:
            return
        self._collect(raise_errors=raise_errors)
        if not dispatch or self.stop_event.is_set() or self.error is not None:
            return
        for provider, queue in self.queues.items():
            if not queue or provider in self.running or provider in exclude:
                continue
            try:
                self.check()
            except Exception as exc:
                self.error = self.error or exc
                self.stop_event.set()
                if raise_errors:
                    raise
                return  # A higher-priority final audit may have a later reserve.
            lease = self.lanes.try_image(provider)
            if lease is None:
                continue
            try:
                reserved = self.reserve(queue.popleft())
                def work(job=copy.deepcopy(reserved), held=lease):
                    with held:
                        return self.generate(job, self.stop_event)
                future = self.executor.submit(work)
                self.running[provider] = (future, reserved)
            except Exception:
                lease.release()
                raise

    def close(self, *, cancel=False):
        if self.closed:
            return
        if cancel:
            self.stop_event.set()
        for queue in self.queues.values():
            queue.clear()
        # Each running child has a bounded timeout and the batch stop signal.
        self.executor.shutdown(wait=True, cancel_futures=False)
        self._collect(raise_errors=False)
        self.closed = True
