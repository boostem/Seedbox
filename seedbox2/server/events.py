"""The event stream: any thread publishes `{type, data}` events; each `/api/events` client drains its own queue.

Ported from seedbox/events.py (Seedbox 1), so the frontend's EventSource handling carries over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable

log = logging.getLogger(__name__)

HEARTBEAT_S = 15.0
QUEUE_SIZE = 1000  # per client; on overflow the oldest events are dropped (a stalled client can't eat memory)

_CLOSE = None  # sentinel that ends a stream


class _Subscriber:
    """One client: its event loop and a bounded queue of encoded events. `push` runs on that loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize)
        self.dropped = 0

    def push(self, item: str | None) -> None:
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass


class EventBus:
    """Fan-out of `{type, data}` events to every connected `/api/events` client."""

    def __init__(self, queue_size: int = QUEUE_SIZE, heartbeat_s: float = HEARTBEAT_S) -> None:
        self.queue_size = queue_size
        self.heartbeat_s = heartbeat_s
        self._subs: set[_Subscriber] = set()
        self._lock = threading.Lock()
        self._closed = False

    def publish(self, type_: str, data: object = None) -> None:
        """Send one event to every client. Safe from any thread; never blocks, never raises."""
        try:
            line = json.dumps({"type": type_, "data": data}, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            log.warning("dropping unencodable %s event: %s", type_, e)
            return
        self._broadcast(line)

    def _broadcast(self, item: str | None) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub.push, item)
            except RuntimeError:  # that client's loop is gone
                self._discard(sub)

    def _discard(self, sub: _Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._subs)

    def close(self) -> None:
        """End every open stream (server shutdown); later subscribers get an immediately-closed stream."""
        with self._lock:
            self._closed = True
        self._broadcast(_CLOSE)

    async def stream(self, until_disconnected: Callable[[], Awaitable[None]] | None = None) -> AsyncIterator[str]:
        """SSE body: `data: <json>` per event, a comment line every `heartbeat_s`.

        `until_disconnected` returns when the client goes away; the stream then ends and unsubscribes at once.
        """
        sub = _Subscriber(asyncio.get_running_loop(), self.queue_size)
        with self._lock:
            closed = self._closed
            if not closed:
                self._subs.add(sub)
        if closed:
            return
        watcher = asyncio.ensure_future(until_disconnected()) if until_disconnected is not None else None
        getter: asyncio.Future | None = None
        try:
            yield "retry: 2000\n: connected\n\n"
            while True:
                getter = asyncio.ensure_future(sub.queue.get())
                waiting = {getter} if watcher is None else {getter, watcher}
                done, _ = await asyncio.wait(waiting, timeout=self.heartbeat_s, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    if watcher is not None and watcher in done:
                        break
                    yield ": heartbeat\n\n"
                    continue
                item = getter.result()
                if item is _CLOSE:
                    break
                yield f"data: {item}\n\n"
        finally:  # also reached when the response task is cancelled mid-wait (client gone, shutdown)
            for task in (getter, watcher):
                if task is not None and not task.done():
                    task.cancel()
            self._discard(sub)
